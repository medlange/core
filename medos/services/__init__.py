# SPDX-License-Identifier: Apache-2.0
"""The Service Plane (chapter 2 section 2.1).

`MOS-SVC-002`: "The Service Plane MUST be the only plane that contains vendor-supplied
code, and vendor-supplied code MUST NOT run in any other plane." Everything under this
package is a `ServiceVersion`'s code -- it consumes a canonical volume and returns findings
and label maps, and it MUST NOT hold a PACS credential, touch PostgreSQL, consume the event
bus, reach the object store, or write a DICOM object (section 2.7's ownership table).

`MOS-REL-020` is the reason this directory exists as a sibling of `medos/medos/` rather than a
subpackage of it: the 0.3.0 `zero-core-change` gate asserts that the diff introducing a
second capability touches only `medos/services/`, `medos/schemas/`, `medos/examples/` and registry rows.
"""
