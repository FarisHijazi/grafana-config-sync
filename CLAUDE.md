# grafana-config-sync

Usage, layout and the Grafana traps: `README.md`. Rule files go in `provisioning/alerting/`; `alerting/`, `instance/`, `dashboards/` are generated and gitignored (they can hold decrypted secrets). Test: `uv run pytest` + `uv run ruff check --no-fix`; real check = `scripts/grafana-sync.py status` against a throwaway `grafana/grafana:12.4.2` container.
