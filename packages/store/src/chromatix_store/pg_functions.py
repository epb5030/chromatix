"""PostgreSQL write functions (SECURITY DEFINER): names and signatures.

The SQL itself lives in the migrations (``migrations/versions/``), frozen per
revision.  On a shared store, client roles may only SELECT from tables and
call the public functions below.  Each function runs with the owner's
privileges, identifies the caller from ``session_user`` (so attribution comes
from PostgreSQL authentication, not from anything the client sends), enforces
the reason rules, and writes the data row(s) together with their audit event.

Error codes raised by the functions (mapped to Python exceptions by the store):

* ``CXU01`` - the session user has no active ChromaTix account
* ``CXR01`` - a reason is required (changing a method, reprocessing)
"""

from __future__ import annotations

PUBLIC_FUNCTIONS = (
    "chromatix_create_project(text, text)",
    "chromatix_create_sample_set(integer, text)",
    "chromatix_register_raw_file(text, bigint, text, text)",
    "chromatix_add_injection(integer, text, text, text, text, double precision)",
    "chromatix_save_method_version(integer, text, jsonb, text)",
    "chromatix_save_result(integer, integer, text, jsonb)",
)
INTERNAL_FUNCTIONS = (
    "chromatix_current_user()",
    "chromatix_audit(text, text, text, text, jsonb)",
)
