INFRA := .infra.yml
PROJECT := $(shell yq '.project.name' $(INFRA))
DB_NAME := $(shell yq '.project.db_name' $(INFRA))
PORT := $(shell yq '.project.port' $(INFRA))
DEPLOY_PATH := $(shell yq '.project.deploy_path' $(INFRA))
HEALTH_URL := $(shell yq '.project.health_url' $(INFRA))
INFRA_REPO := $(HOME)/p/franklin-infra
FRANKLIN := clayarnold@100.112.120.2

.PHONY: deploy rollback migrate migrate-franklin migrate-create logs backup restore status health deploy-branch teardown-branch

deploy:
	ssh $(FRANKLIN) "cd $(DEPLOY_PATH) && git pull origin main && docker compose up -d --build"

rollback:
	@echo "Available deploy tags:"
	@git tag -l 'deploy-*' --sort=-creatordate | head -10
	@read -p "Tag to rollback to: " TAG && \
	if [ -z "$$(git tag -l "$$TAG")" ]; then \
		echo "Error: tag '$$TAG' does not exist."; \
		exit 1; \
	fi && \
	ssh $(FRANKLIN) "cd $(DEPLOY_PATH) && git fetch --tags && git checkout $$TAG && docker compose up -d --build"

migrate:
	migrate -path migrations -database "postgresql://postgres@127.0.0.1:5433/$(DB_NAME)?sslmode=disable" up

migrate-franklin:
	ssh $(FRANKLIN) "mkdir -p $(DEPLOY_PATH)/migrations"
	scp migrations/* $(FRANKLIN):$(DEPLOY_PATH)/migrations/
	ssh $(FRANKLIN) "cd $(DEPLOY_PATH) && migrate -path migrations -database 'postgresql://postgres@127.0.0.1:5433/$(DB_NAME)?sslmode=disable' up"

migrate-create:
	@read -p "Migration name: " NAME && \
	migrate create -ext sql -dir migrations -seq $$NAME

logs:
	ssh $(FRANKLIN) "cd $(DEPLOY_PATH) && docker compose logs --tail=100 -f"

backup:
	ssh $(FRANKLIN) "mkdir -p ~/backups/$(DB_NAME) && pg_dump -h 127.0.0.1 -p 5433 -U postgres $(DB_NAME) | gzip > ~/backups/$(DB_NAME)/$(DB_NAME)_$$(date +%Y%m%d_%H%M%S).sql.gz"
	@echo "Backup created on Franklin"

restore:
	@echo "Available backups:"
	@ssh $(FRANKLIN) "ls -lht ~/backups/$(DB_NAME)/ | head -10"
	@read -p "Filename to restore: " FILE && \
	ssh $(FRANKLIN) "gunzip -c ~/backups/$(DB_NAME)/$$FILE | psql -h 127.0.0.1 -p 5433 -U postgres $(DB_NAME)"

status:
	ssh $(FRANKLIN) "cd $(DEPLOY_PATH) && docker compose ps"

health:
	@curl -sf --max-time 10 $(HEALTH_URL) > /dev/null \
		&& echo "✓ $(PROJECT) is healthy" \
		|| echo "✗ $(PROJECT) is unreachable"

deploy-branch:
	@test -n "$(BRANCH)" || (echo "Usage: make deploy-branch BRANCH=staging" && exit 1)
	$(INFRA_REPO)/scripts/branch-deploy.sh deploy . $(BRANCH)

teardown-branch:
	@test -n "$(BRANCH)" || (echo "Usage: make teardown-branch BRANCH=staging" && exit 1)
	$(INFRA_REPO)/scripts/branch-deploy.sh teardown . $(BRANCH)
