# SPDX-License-Identifier: Apache-2.0
"""Shared test-harness support for the MedicalOS suite.

Nothing in here is production code and nothing in `medos/medos/` may import it.

`tests._support.skips` is the ONE home of the skip taxonomy: every `pytest.skip`
in this repository goes through `skip_infra`, `skip_no_data` or `skip_environment`
so that a run can say *what* it did not test, and so a CI job can refuse to be
green on a system that could not serve a request.
"""
