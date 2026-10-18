-- Production champion definition (warehouse). Table and column names are placeholders:
-- map them to your CRM + product-usage models. The public demo uses case-study evidence instead.
--
-- A champion decided AND still uses the product:
--   a won-deal contact role (Decision Maker, Champion, Economic Buyer, Evaluator) on a deal AT THE CONTACT'S OWN
--   ACCOUNT closed in the last 3 years, AND product use by that contact at that account in the last 365 days;
--   OR an active product admin (admin activity in the last 180 days) who also holds such a role (any close date).
-- An admin seat alone is a 'user', evidence 'product admin, no decision or ownership record': a title or a seat never
-- makes a champion. Everyone else with a won-deal role is a 'user' (reference value, no play when they move).
-- Someone who only sat on the deal and never used the product is 'user', evidence 'role only, no product use'.
-- Usage that counts: no test workspaces, no internal users (a NULL flag counts as real usage, not as test).
-- Output: one row per person, with the evidence and its dates. last_activity = the contact's latest product use at
-- the customer; evidence_at = the latest dated evidence (deal close or product use).
--
-- ACCOUNT IDS: product.users.account_id MUST be the CRM account id (crm.account.id). If the product keys accounts by
-- its own tenant / workspace id, map it through a product-to-CRM table first (see the commented join in product_use);
-- without that, every role champion silently becomes 'role only, no product use'.
-- Kept in sync with champion_definition.sqlite.sql (the demo); tests run that copy.

with won_deal_contacts as (                         -- one row per contact: their latest won deal at their own account
    select
        ocr.contact_id,
        o.account_id,
        ocr.role                                     as contact_role,      -- e.g. 'Decision Maker', 'Champion', 'Economic Buyer'
        o.close_date
    from crm.opportunity_contact_role ocr
    join crm.opportunity o on o.id = ocr.opportunity_id
    join crm.contact c on c.id = ocr.contact_id
    where o.is_won
      and o.account_id = c.account_id                -- the role and the product use come from the same account
      and ocr.role in ('Decision Maker', 'Champion', 'Economic Buyer', 'Evaluator')
    qualify row_number() over (partition by ocr.contact_id order by o.close_date desc, o.id) = 1   -- id breaks ties
),

product_use as (                                    -- per contact and CRM account: latest real use, latest admin use
    select
        u.contact_id,
        u.account_id,                                -- must be the CRM account id; else use m.crm_account_id below
        max(e.event_at)                                       as last_activity,
        max(case when u.is_admin then e.event_at end)         as last_admin_activity
    from product.users u
    -- join map.product_account_to_crm m on m.product_account_id = u.account_id   -- product tenant id -> CRM account id
    join product.events e on e.user_id = u.id
    where coalesce(e.is_test_workspace, false) = false
      and coalesce(e.is_internal_user, false) = false
    group by 1, 2
),

people as (
    select
        c.id                                          as contact_id,
        c.first_name,                                 -- CRM FirstName / LastName: never split full_name on a space
        c.last_name,
        c.full_name,
        c.title,
        c.linkedin_url,
        a.name                                        as customer_name,
        a.website_domain                              as customer_domain,
        w.contact_role,
        w.close_date,
        pu.last_activity,
        pu.last_admin_activity,
        coalesce(w.contact_role is not null
                 and w.close_date >= dateadd(year, -3, current_date)
                 and pu.last_activity >= dateadd(day, -365, current_date), false)    as is_role_champion,
        coalesce(pu.last_admin_activity >= dateadd(day, -180, current_date), false) as is_active_admin
    from crm.contact c
    join crm.account a on a.id = c.account_id
    left join won_deal_contacts w on w.contact_id = c.id
    left join product_use pu on pu.contact_id = c.id and pu.account_id = c.account_id   -- use at the customer
    where w.contact_id is not null or pu.last_admin_activity is not null
)

select
    contact_id, first_name, last_name, full_name, title, linkedin_url, customer_name, customer_domain,
    case when contact_role is not null then contact_role else 'Admin' end           as evidence_type,
    case when is_role_champion or (is_active_admin and contact_role is not null) then 'champion' else 'user' end as level,
    case
        when is_role_champion then 'won-deal role and product use in the last 365 days'
        when is_active_admin and contact_role is not null then 'product admin active in the last 180 days, with a won-deal role'
        when contact_role is not null and last_activity is null then 'role only, no product use'
        when contact_role is not null and close_date < dateadd(year, -3, current_date) then 'won deal closed over 3 years ago'
        when contact_role is not null then 'role, but no product use in the last 365 days'
        when is_active_admin then 'product admin, no decision or ownership record'
        else 'admin, but no admin activity in the last 180 days'
    end                                               as evidence,
    close_date                                        as deal_closed,
    last_admin_activity,
    last_activity,
    greatest(coalesce(close_date, '1900-01-01'::date),
             coalesce(last_activity::date, '1900-01-01'::date))           as evidence_at
from people;
