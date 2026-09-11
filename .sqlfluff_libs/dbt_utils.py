"""Lint-time stub for the dbt_utils package namespace.

sqlfluff's `jinja` templater (not the `dbt` templater -- see ADR-002 in
docs/data_modeling_decisions.md for why this project uses `jinja`, not
`dbt`) has no concept of an installed dbt package. `apply_dbt_builtins`
mocks `ref()`/`source()`/`var()`/etc., but a package macro called via its
namespace (`dbt_utils.generate_surrogate_key(...)`) is just an undefined
`dbt_utils` name to sqlfluff, which fails to template (`TMP` rule) rather
than falling back to a silent placeholder the way `var()`/`ref()` do.

`.sqlfluff`'s `library_path` points at this directory; sqlfluff imports
every top-level module here and exposes it in the Jinja context by its
filename, so `dbt_utils.py` becomes the `dbt_utils` name used at any model
call site. Add a function here for any other dbt_utils macro this project
starts calling by namespace -- this file only stubs what's actually used
today (`generate_surrogate_key`, see ADR-007), matching this project's
own discipline against building out unused surface area.
"""


def generate_surrogate_key(field_list):
    """Render as a harmless, syntactically valid scalar expression.

    The real macro (dbt/dbt_packages/dbt_utils/macros/sql/generate_surrogate_key.sql)
    coalesces/concatenates/hashes the given fields. Lint only needs
    *something* that parses as a scalar SQL expression at the call site --
    it never executes the rendered SQL -- so the field list itself is
    discarded here.
    """
    return "'dbt_utils_generate_surrogate_key_lint_stub'"
