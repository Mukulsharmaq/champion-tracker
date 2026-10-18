-- Production champion definition (warehouse). Table and column names are placeholders:
-- map them to your CRM + product-usage models. The public demo uses case-study evidence instead.
-- Output: one row per person who influenced or owns the product at a customer, with the evidence that says so.

with won_deal_contacts as (
    select
        ocr.contact_id,
        o.account_id,
        ocr.role                                     as contact_role,      -- e.g. 'Decision Maker', 'Champion', 'Economic Buyer'
        o.close_date
    from crm.opportunity_contact_role ocr
    join crm.opportunity o on o.id = ocr.opportunity_id
    where o.is_won
      and ocr.role in ('Decision Maker', 'Champion', 'Economic Buyer', 'Evaluator')
),

active_admins as (
    select
        u.contact_id,
        u.account_id,
        max(e.event_at)                              as last_admin_activity
    from product.users u
    join product.events e on e.user_id = u.id
    where u.is_admin
      and not e.is_test_workspace                  -- usage that counts: exclude test and internal workspaces
      and not e.is_internal_user
    group by 1, 2
),

champions as (
    select
        c.id                                          as contact_id,
        c.full_name,
        c.title,
        c.linkedin_url,
        a.name                                        as customer_name,
        a.website_domain                              as customer_domain,
        coalesce(w.contact_role, 'Admin')             as evidence_type,
        case
            when w.contact_role is not null then 'champion'
            when ad.last_admin_activity >= dateadd(day, -180, current_date) then 'champion'
            else 'user'
        end                                           as level,
        greatest(coalesce(w.close_date, '1900-01-01'), coalesce(ad.last_admin_activity, '1900-01-01')) as evidence_at
    from crm.contact c
    join crm.account a on a.id = c.account_id
    left join won_deal_contacts w on w.contact_id = c.id
    left join active_admins ad on ad.contact_id = c.id
    where w.contact_id is not null or ad.contact_id is not null
)

select * from champions;
