.PHONY: build ci publish

VERSION := $(shell cd backend && uv version --short)
ARTIFACT := vercel_hatchery-$(VERSION)

build:
	cd backend/frontend && pnpm install --frozen-lockfile && pnpm build
	cd backend && uv build

ci: build
	cd backend && uv lock --check && uv run --locked --python 3.14 pytest -q
	cd backend/frontend && pnpm test && pnpm lint

# Publish only previously built artifacts from the OIDC-enabled GitHub Actions job.
publish:
	cd backend && uv publish --trusted-publishing always dist/$(ARTIFACT).tar.gz dist/$(ARTIFACT)-py3-none-any.whl
