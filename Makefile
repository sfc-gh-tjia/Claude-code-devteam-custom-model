PY ?= python3
REF = $(PY) harness/referee.py
PORT ?= 4000

.PHONY: doctor verify status ledger reap tokens harness-test gateway gateway-stop gateway-status probe probe-identity profile tiers

doctor:            ## preflight: gateway, deps, ports, credentials, git state
	$(REF) --strict doctor

verify:            ## every gate on the current tree (the definition of done)
	$(REF) --strict --inline gate

status:            ## task states, small JSON
	$(REF) status

ledger:            ## regenerate work/ledger.md
	$(REF) ledger

reap:              ## remove merged worktrees, branches and logs
	$(REF) reap

tokens:            ## token/cost telemetry from Claude Code transcripts (DeepSeek $, NVIDIA free, v1 equivalent)
	$(PY) harness/tokens.py

gateway:           ## start the DeepSeek/NVIDIA inference gateway in the background (.loop/gateway.log)
	@curl -fs http://127.0.0.1:$(PORT)/health/liveliness >/dev/null 2>&1 && echo "gateway already up on :$(PORT)" || gateway/start.sh --daemon

gateway-stop:      ## stop the gateway
	@test -f .loop/gateway.pid && kill $$(cat .loop/gateway.pid) && rm -f .loop/gateway.pid && echo stopped || echo "not running"

gateway-status:    ## is the gateway up?
	@curl -fs http://127.0.0.1:$(PORT)/health/liveliness >/dev/null && echo "up on :$(PORT)" || (echo "down: run make gateway"; exit 1)

probe:             ## live check of every route with your real keys (< 1 cent)
	$(PY) gateway/probe.py

probe-identity:    ## is ds-pro really a different model from ds-flash? (direct to DeepSeek, a few cents)
	$(PY) gateway/probe.py --identity

profile:           ## show the active profile's role -> tier map (PROFILE=<name> to preview another)
	@cd gateway && PROFILE=$${PROFILE:-$$(grep -s '^PROFILE=' .env | cut -d= -f2)} $(PY) -c "import hooks,tiers;t=tiers.load_tiers();n,r=hooks.active_profile();print('profile',n);[print(f'  role-{k:16} -> {v:13} {t[v][\"model\"]:52} {t[v][\"data\"]}') for k,v in sorted(r.items())]"

tiers:             ## validate gateway/tiers.toml and the active profile, and render the gateway config
	$(PY) gateway/render.py

harness-test:      ## test the harness and the gateway (no API keys needed)
	$(PY) -m pytest -q -p no:cacheprovider harness/tests gateway/tests
