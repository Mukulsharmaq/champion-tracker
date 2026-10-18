-- Same definition as champion_definition.sql, in SQLite for the demo warehouse. :today is pinned by the caller.
-- Champion = (won-deal role on a deal at the contact's own account, closed in the last 3 years, AND product use in
--             the last 365 days) OR (product admin active in the last 180 days who also holds such a role).
-- An admin seat alone is a 'user' ('product admin, no decision or ownership record'); a role with no product use is
-- a 'user'. product_users.account_id must be the CRM account id (map a product tenant id first; see the main file).
with won_deal_contacts as (                         -- one row per contact: their latest won deal at their own account
    select contact_id, account_id, contact_role, close_date from (
        select ocr.contact_id, o.account_id, ocr.role as contact_role, o.close_date,
               row_number() over (partition by ocr.contact_id order by o.close_date desc, o.id) as rn   -- id breaks ties
        from crm_opportunity_contact_role ocr
        join crm_opportunity o on o.id = ocr.opportunity_id
        join crm_contact c on c.id = ocr.contact_id
        where o.is_won = 1
          and o.account_id = c.account_id            -- the role and the product use come from the same account
          and ocr.role in ('Decision Maker', 'Champion', 'Economic Buyer', 'Evaluator')
    ) where rn = 1
),

product_use as (                                    -- per contact and CRM account: latest real use, latest admin use
    select u.contact_id, u.account_id,              -- must be the CRM account id; else use m.crm_account_id below
           max(e.event_at) as last_activity,
           max(case when u.is_admin = 1 then e.event_at end) as last_admin_activity
    from product_users u
    -- join product_account_to_crm m on m.product_account_id = u.account_id   -- product tenant id -> CRM account id
    join product_events e on e.user_id = u.id
    where coalesce(e.is_test_workspace, 0) = 0      -- usage that counts: no test or internal workspaces
      and coalesce(e.is_internal_user, 0) = 0       -- (a NULL flag counts as real usage)
    group by 1, 2
),

people as (
    select
        c.id as contact_id, c.first_name, c.last_name, c.full_name, c.title, c.linkedin_url,
        a.name as customer_name, a.website_domain as customer_domain,
        w.contact_role, w.close_date, pu.last_activity, pu.last_admin_activity,
        coalesce(w.contact_role is not null
                 and w.close_date >= date(:today, '-3 years')
                 and pu.last_activity >= date(:today, '-365 days'), 0) as is_role_champion,
        coalesce(pu.last_admin_activity >= date(:today, '-180 days'), 0) as is_active_admin
    from crm_contact c
    join crm_account a on a.id = c.account_id
    left join won_deal_contacts w on w.contact_id = c.id
    left join product_use pu on pu.contact_id = c.id and pu.account_id = c.account_id   -- use at the customer
    where w.contact_id is not null or pu.last_admin_activity is not null
)

select
    contact_id, first_name, last_name, full_name, title, linkedin_url, customer_name, customer_domain,
    case when contact_role is not null then contact_role else 'Admin' end as evidence_type,
    case when is_role_champion or (is_active_admin and contact_role is not null) then 'champion' else 'user' end as level,
    case
        when is_role_champion then 'won-deal role and product use in the last 365 days'
        when is_active_admin and contact_role is not null then 'product admin active in the last 180 days, with a won-deal role'
        when contact_role is not null and last_activity is null then 'role only, no product use'
        when contact_role is not null and close_date < date(:today, '-3 years') then 'won deal closed over 3 years ago'
        when contact_role is not null then 'role, but no product use in the last 365 days'
        when is_active_admin then 'product admin, no decision or ownership record'
        else 'admin, but no admin activity in the last 180 days'
    end as evidence,
    close_date as deal_closed,
    last_admin_activity,
    last_activity,
    max(coalesce(close_date, '1900-01-01'), coalesce(substr(last_activity, 1, 10), '1900-01-01')) as evidence_at
from people
order by customer_name, full_name;
