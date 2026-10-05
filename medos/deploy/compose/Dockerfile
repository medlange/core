# =====================================================================================
# MedicalOS weeks 1-2 vertical slice -- the one Python image.
#
# ONE image, two commands. `medos-api` runs `uvicorn medos.api.app:create_app --factory`
# and `medos-worker` runs `python -m medos.worker.runner`. They are the same bytes because
# they are the same program (CONTRACT.md section 0: "the HTTP layer MUST stay thin -- no
# business logic in handlers"); the worker imports `medos.core`, `medos.capabilities` and
# `medos.writer`, the API imports `medos.db`, and both sit on the same `medos` package.
# Two images would mean two dependency sets to keep in step for no gain a compose stack
# can observe.
#
# NOT in this image, on purpose (CONTRACT.md section 0): no torch, no MONAI, no CUDA, no
# Triton client. The three capabilities of CONTRACT.md section 7 are a HU threshold, a
# percent-below-threshold count and a placeholder -- numpy is the whole inference runtime.
# A CUDA base layer here would be 4 GB of image asserting a capability the slice does not
# have, and MOS-REL-050 calls that the forbidden kind of stub.
#
# Pinned to the patch (MOS-TEST-068's spirit; see medos/deploy/compose/docker-compose.yml's
# IMAGE PINNING note): `python:3.11-slim` floats and would make a green run
# unreproducible three weeks later.
# =====================================================================================
FROM python:3.11.16-slim-bookworm AS train

# PYTHONDONTWRITEBYTECODE: the app directory is read-only in the compose stack.
# PYTHONUNBUFFERED: CONTRACT.md section 11 wants structured JSON logs; a buffered stdout
#   turns "the worker logged nothing" into an unfalsifiable statement about the worker.
# PYTHONFAULTHANDLER: a segfault in a numpy extension otherwise exits 139 with no trace.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, in their own layer: `medos/medos/` changes on every edit and the wheel
# install does not. The versions are the floors from pyproject.toml, pinned to exact
# releases here because an image is a deployment artifact and a floor is not a pin.
# psycopg[binary] rather than psycopg[c]: the binary wheel carries its own libpq, so the
# image needs no build toolchain and no libpq-dev.
COPY medos/deploy/compose/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# The package and its definition. Nothing else from the repo: `docs/` is 2.2 MB of
# specification and `tests/` runs on the host against the published ports. See
# Dockerfile.dockerignore.
COPY pyproject.toml /app/
# The package and its definition, INCLUDING THE SDK: `medos/medos/sdk/` is the package
# `MOS-IMG-003`'s contracts moved into the distribution (`medos.sdk`), so the single COPY
# of `medos/medos` carries both the platform and the SDK. Nothing else from the repo:
# `docs/` is 2.2 MB of specification and `tests/` runs on the host against the published
# ports. See Dockerfile.dockerignore.
COPY medos/medos /app/medos

# The publish half of the packaging step (weeks 3-5, docs/spec/15-delivery.md 15.2.4).
# ONE file out of medos/tools/, deliberately: `medos/tools/publish_model.py publish` uploads weights
# that are bind-mounted from the host, and `... build` -- which needs `onnx` and produces
# them -- is never runnable in this image, because MOS-OPS-071 requires the serving
# platform to be incapable of performing a backend conversion. Copying the script is
# copying CODE; the weights stay outside the image (MOS-SVC-058).
COPY medos/tools/publish_model.py /app/tools/publish_model.py

# The demo seeder, the SECOND and last file out of medos/tools/. It fabricates a synthetic CT
# study so a first `up` shows something other than an empty archive. It lives in medos/tools/
# rather than medos/medos/ because MOS-IMG-069 forbids the platform to mint a
# StudyInstanceUID, and it refuses to run unless MEDOS_ENV is exactly `dev` -- so
# shipping it in the serving image adds a capability that an operator cannot accidentally
# invoke against patients. Copying the script is copying CODE; it carries no images.
COPY medos/tools/demo/seed_corpus.py /app/tools/demo/seed_corpus.py

# =====================================================================================
# THE SERVICE PLANE. `medos/services/` is vendor code (MOS-SVC-002) and is deliberately NOT part
# of the `medos` distribution -- `[tool.setuptools.packages.find] include = ["medos*"]`
# does not cover it, and widening that would make third-party capabilities ship inside the
# platform wheel, which is the opposite of the boundary chapter 2 section 2.1 draws.
#
# So it is COPIED and put on the import path, and PYTHONPATH is the honest spelling of
# "this is beside the platform, not inside it". `medos.capabilities.providers` imports a
# provider by dotted path at startup; without this, `MEDOS_CAPABILITY_PROVIDERS` in
# docker-compose.yml would refuse the container with "did not import", which is the correct
# failure and not one to ship in.
#
# `medos/examples/lung-nodule/` comes with it because it is that capability's REGISTRY DATA --
# `capability.json` is the applicability envelope the class evaluates at run time and
# `concepts.overlay.json` is its coded-concept rows, which `services.lung_nodule.concepts`
# resolves relative to the repository root. Data, not documentation.
#
# REPORTED, NOT HIDDEN: a real deployment installs each Service Plane package as its own
# signed wheel or (chapter 2 section 2.4) its own OCI artifact, and does not bake a vendor's
# source into the platform image at all. This stack has one first-party capability under
# `medos/services/` and no registry to resolve an artifact from, so the copy is the stand-in.
COPY medos/services /app/services
COPY medos/examples/lung-nodule /app/examples/lung-nodule
ENV PYTHONPATH=/app

# NO COPY FOR THE CODED-CONCEPT DICTIONARY ANY MORE, AND THAT IS THE POINT.
#
# `capability_concepts.json` used to live in `spikes/week0/` and this file copied it to
# `/app/share/` with a `MEDOS_CAPABILITY_CONCEPTS` override, because the path the platform
# resolved was outside the distribution and did not exist in an image shipping only the
# `medos` package. `MOS-IMG-112` forbids inventing a code and `MOS-IMG-113` forbids
# materialising the list in Python, so without the file `medos.writer` refuses to write at
# all -- a two-line workaround standing between the image and a correct failure.
#
# The file is now `medos/medos/capabilities/capability_concepts.json`, declared as package
# data, so `pip install` below carries it. Still exactly one copy. The override env var
# still exists and is still how a deployment points at a real `capability_concepts` table
# export (`MOS-IMG-111`); it is simply no longer load-bearing for the image to start.

# Installed, not merely copied onto PYTHONPATH: `pip install .` is what proves
# pyproject.toml's `[tool.setuptools.packages.find]` actually resolves, and an image that
# only works because of a PYTHONPATH env var hides a broken package definition until the
# first person tries to `pip install medos`.
RUN pip install --no-cache-dir --no-deps -e .

# A non-root uid. No tenancy and no auth in this slice (CONTRACT.md section 0), so this is
# not a security boundary -- it is the ordinary container hygiene that makes the eventual
# `read_only: true` root filesystem possible without a rewrite.
#
# /models exists in the IMAGE, empty and owned by medos, for one specific reason: Docker
# initialises an empty named volume from the image path it is mounted over, ownership
# included. Without this directory the `model-repo` volume is created root-owned and
# medos-tritond -- which runs as uid 10001 and has to WRITE chapter 13 13.10.1's model
# repository into it -- fails with EACCES on its first stage(). The directory is empty in
# the image and stays empty: MOS-SVC-058 forbids baking weights in, and every byte under
# /models at runtime arrives from the object store.
RUN useradd --create-home --uid 10001 medos \
    && mkdir -p /var/lib/medos/work /models \
    && chown -R medos:medos /var/lib/medos /models
USER medos

# The worker's scratch directory for WADO-retrieved instances. A named volume in compose;
# declared here so `docker run` without one still works.
ENV MEDOS_WORK_ROOT=/var/lib/medos/work

# No ENTRYPOINT: compose gives each service its own `command`, and an entrypoint that
# wrapped both would be a shell script nobody tests.
CMD ["python", "-c", "import medos, sys; print('medos image ok', sys.version)"]


# =====================================================================================
# core -- the PACS-and-models image, and the DEFAULT target.
#
# TWO PRODUCTS, TWO IMAGES. The stage above is `train`: the model-preparation service,
# which carries the cohort, curation, seal and conversion machinery. This stage is the
# service a site runs when it only wants to SERVE models, and the difference between
# them is not configuration -- it is that the files are not here.
#
# `tests/gate/test_core_train_boundary.py` already asserts that `medos.api.app`'s static
# import closure never reaches `medos.training`. That check is about the SOURCE. This
# stage is about the ARTEFACT, and they fail for different reasons: the gate catches a
# developer adding an import, this catches an image built from a tree where the gate was
# not run. A claim that a service cannot train is worth more when the code that would
# train is absent from its filesystem.
#
# HONEST ABOUT THE LIMIT: `rm` in a derived stage removes the paths from THIS stage's
# filesystem, which is what an operator, a scanner and the Python import system all see.
# The bytes still exist in the parent layer and `docker history` will show them. This is
# a segregation boundary, not a secrecy one -- nothing in `medos/medos/training` is a secret,
# and squashing layers to hide it would trade an auditable history for a cosmetic size
# win.
#
# The last stage in the file is the default target, so a plain `docker build` produces
# CORE. Building the training service is explicit: `--target train`.
# =====================================================================================
FROM train AS core

USER root
RUN rm -rf /app/medos/training            /app/medos/api/routes_training.py            /app/medos/api/routes_curation.py            /app/medos/api/training_plane.py  && find /app/medos -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
USER medos

# THE BUILD FAILS IF CORE CANNOT STAND ALONE. Not a comment claiming the removal is
# safe: the app factory is constructed here, in the image, with the training plane
# absent. A stray import anywhere in Core's closure is a broken build rather than a
# container that crashes on its first request.
#
# It also asserts the training plane is really gone -- an `rm` whose path was wrong
# would otherwise leave a Core image that passes every check by still containing
# everything.
# The path walk unwraps Starlette 1.x's lazy `_IncludedRouter` proxies (the `.routes`
# list then holds proxies whose `original_router` carries the real routes); on older
# Starlette lines `original_router` is absent and the route itself is used as-is.
RUN python -c "import importlib.util; from medos.api.app import create_app; app = create_app(connect=lambda **k: None); routes = [x for r in app.routes for x in (getattr(r, 'original_router', None).routes if getattr(r, 'original_router', None) is not None else [r])]; paths = {r.path for r in routes if getattr(r, 'path', '').startswith('/api/v1')}; assert paths, 'core app served no /api/v1 routes'; leaked = sorted(p for p in paths if 'training-runs' in p or 'harvest' in p); assert not leaked, f'core image serves model-preparation routes: {leaked}'; assert importlib.util.find_spec('medos.training') is None, 'medos.training is still in the core image'; print(f'core image ok: {len(paths)} /api/v1 paths, no training plane')"

CMD ["python", "-c", "import medos, sys; print('medos core image ok', sys.version)"]
