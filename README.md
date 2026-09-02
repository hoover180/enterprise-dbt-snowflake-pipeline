# GlobalRetail Co. — Multi-Source Customer & Revenue Reconciliation Platform

**Status: in development.** This README will fill in as each phase lands — no placeholder metrics are published until they're real.

## Business Problem

"GlobalRetail Co." is a fictional omnichannel retailer whose customer and revenue data lives in three systems that were never designed to agree with each other: a regionally-sharded ERP that overwrites order status with no history, a web analytics pipeline that changed its own event schema mid-year, and a CRM full of manually-entered inconsistency. Finance can't produce a trusted revenue number without a manual reconciliation exercise every close, and no team can answer a simple question: who is this customer, across every channel they touch us on?

This platform ingests all three sources, resolves their conflicts with documented logic, and publishes a reconciled Customer 360 + revenue view.

## Stack

Snowflake · dbt-core · Terraform · GitHub Actions · Tableau · Streamlit in Snowflake · Python (Faker)

## Related Portfolio Projects

- [fabric-ecommerce-analytics](https://github.com/hoover180/fabric-ecommerce-analytics) — Microsoft-native BI delivery, single source, Kimball modeling
- [financial-market-data-pipeline](https://github.com/hoover180/financial-market-data-pipeline) — platform/reliability engineering on Databricks
- **This project** — cross-source truth and reconciliation on Snowflake

---

*Michael Hoover · [LinkedIn](https://linkedin.com/in/michael-hoover-365data) · [GitHub](https://github.com/hoover180)*
