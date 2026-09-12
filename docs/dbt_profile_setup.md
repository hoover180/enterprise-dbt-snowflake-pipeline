# Local dbt Profile Setup

`~/.dbt/profiles.yml` is never committed to this repo (it's git-ignored on
principle even though it holds no secret itself — the private key it points
at is the actual secret, kept outside the repo entirely under
`~/.snowflake/keys/`). Each contributor creates their own.

## Required entry

Add (or merge, if the file already has other projects' profiles) the
following block to `~/.dbt/profiles.yml`:

```yaml
enterprise_dbt_snowflake:
  target: dev
  outputs:
    dev:
      type: snowflake
      account: YOUR_ACCOUNT_LOCATOR
      user: YOUR_SNOWFLAKE_USERNAME
      private_key_path: /path/to/.snowflake/keys/rsa_key.p8
      role: TRANSFORMER_ROLE
      database: DEV_ANALYTICS
      warehouse: TRANSFORM_XS
      schema: RAW
      threads: 4
```

The profile name (`enterprise_dbt_snowflake`) must match the `profile:`
field in [`dbt/dbt_project.yml`](../dbt/dbt_project.yml).

## Why these specific values

| Field               | Value                                     | Why                                                                                                     |
| ------------------- | ------------------------------------------ | --------------------------------------------------------------------------------------------------------- |
| `account`            | `YOUR_ACCOUNT_LOCATOR`                      | Same account Terraform provisions against — see `terraform/variables.tf`.                                  |
| `user`               | `YOUR_SNOWFLAKE_USERNAME`                   | Same Snowflake user Terraform authenticates as — see `terraform/providers.tf`.                              |
| `private_key_path`   | your local `.p8` path                      | Key-pair (JWT) auth, matching `terraform/providers.tf`'s `authenticator = "SNOWFLAKE_JWT"` pattern — no password auth anywhere in this project. |
| `role`               | `TRANSFORMER_ROLE`                          | The least-privilege role Terraform grants CREATE TABLE/VIEW + warehouse USAGE to — see `terraform/roles.tf`. dbt should never run as `ACCOUNTADMIN`. |
| `database`           | `DEV_ANALYTICS`                             | Local development target. `STAGE_ANALYTICS` and `PROD_ANALYTICS` are CI/promotion targets only (auto-deploy on merge to `main`, then a tagged-release approval gate — see `docs/workflow.md`'s Environment Promotion section and Phase 8); nothing here should point a local dev profile at them. |
| `warehouse`          | `TRANSFORM_XS`                              | Smallest warehouse Terraform provisions (`terraform/warehouses.tf`), auto-suspends after 60s idle to control trial credit spend during local iteration. |
| `schema`             | `RAW`                                        | Default target schema. Raw landing tables loaded by `data_gen/load_raw.py` live here; dbt's own `generate_schema_name` macro (once added) will build staging/intermediate/marts schemas off of this default rather than writing models into `RAW` itself. |

## Key-pair auth prerequisite

This assumes you've already generated an RSA key pair and registered the
public key on your Snowflake user (the same key Terraform's
`snowflake_private_key_path` variable points at). If you haven't:

```bash
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out ~/.snowflake/keys/rsa_key.p8 -nocrypt
openssl rsa -in ~/.snowflake/keys/rsa_key.p8 -pubout -out ~/.snowflake/keys/rsa_key.pub
```

Then, as `ACCOUNTADMIN` (or someone who can alter the user), register the
public key:

```sql
ALTER USER YOUR_SNOWFLAKE_USERNAME SET RSA_PUBLIC_KEY='<contents of rsa_key.pub, header/footer stripped>';
```

## Environment variables for `data_gen/load_raw.py`

The raw loader script (`data_gen/load_raw.py`) does not read
`~/.dbt/profiles.yml` — dbt and the Python Snowflake connector each need
their own connection config. Set these before running it (see
`docs/loading_notes.md` for what the script does with them):

| Variable                            | Default            | Notes                                        |
| ------------------------------------ | ------------------- | ---------------------------------------------- |
| `SNOWFLAKE_ACCOUNT`                   | *(required)*         | `YOUR_ACCOUNT_LOCATOR`                         |
| `SNOWFLAKE_USER`                      | *(required)*         | `YOUR_SNOWFLAKE_USERNAME`                      |
| `SNOWFLAKE_PRIVATE_KEY_PATH`           | *(required)*         | Same `.p8` path as the dbt profile above.       |
| `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE`     | *(none)*             | Only needed if the key was generated with `-nocrypt` omitted. |
| `SNOWFLAKE_ROLE`                      | `TRANSFORMER_ROLE`   |                                                |
| `SNOWFLAKE_WAREHOUSE`                  | `TRANSFORM_XS`       |                                                |
| `SNOWFLAKE_DATABASE`                   | `DEV_ANALYTICS`      |                                                |
| `SNOWFLAKE_SCHEMA`                     | `RAW`                |                                                |

The script loads a `.env` file in the repo root if one is present
(`.env` is already git-ignored — see `.gitignore`), so you can keep these
out of your shell profile if you prefer:

```text
SNOWFLAKE_ACCOUNT=YOUR_ACCOUNT_LOCATOR
SNOWFLAKE_USER=YOUR_SNOWFLAKE_USERNAME
SNOWFLAKE_PRIVATE_KEY_PATH=/path/to/.snowflake/keys/rsa_key.p8
```

## Verifying the setup

```bash
dbt debug --project-dir dbt --profiles-dir ~/.dbt
```

This should report a successful connection to `DEV_ANALYTICS` as
`TRANSFORMER_ROLE` using `TRANSFORM_XS`.
