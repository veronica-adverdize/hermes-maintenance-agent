# Hermes Maintenance Agent

A deterministic Python runner that Hermes or another scheduler can invoke to maintain explicitly approved WordPress plugins over MCP. Approval, scope and verification live in code and local configuration. A model never picks a tool or widens the update scope.

## Providers

| Provider | Connection |
| --- | --- |
| WordPress | Included local stdio MCP bridge. Install WP-CLI and run the bridge as the WordPress filesystem owner on a single-site installation. Use the absolute path to the Python executable if a scheduler has a different PATH. |
| Modular DS | Native Streamable HTTP adapter. Supply an MCP bearer token through the named environment variable. Obtain and refresh the token outside this runner, for example through your existing authorized integration. |
| Other maintenance platforms | Expose the standard tools below or implement an adapter around the provider's MCP tools. MCP connectivity alone does not imply a compatible maintenance API. |

## Run

Install Python 3.11+ and run `python -m pip install .`. Copy the relevant file from [examples](examples) to `maintenance.local.json` and enter the site identifiers, approved plugin slugs, installed and target versions, and expected activation states. For Modular DS, include the installation and component identifiers. Keep local configuration, tokens and databases private.

```sh
hermes-maintenance preview --config maintenance.local.json
hermes-maintenance apply --config maintenance.local.json --confirm REVIEWED_SHA256 --audit maintenance.sqlite3
```

Preview reads inventory only and can reflect cached provider data. Apply refreshes each selected site's inventory first, checks the approved versions again, and verifies the installed version and activation state after each update. Use `--site SITE_ID` to run only one configured site. Review the preview digest before supplying it to apply. Keep the same audit database across runs. An unresolved submission stops further writes until the operator reconciles the provider's action and the database. A verified installed target is skipped on later runs. The tool never retries an update submission automatically.

Modular DS selects the offered release at submission time. Its adapter checks the target before submission and verifies the action afterward, but cannot pin the version atomically. Live Modular apply requires the explicit `accept_provider_selected_target` opt-in. An unconfirmed refresh, including a provider cooldown, holds the site for that run.

For Hermes or cron, use the same apply command with a reviewed digest, a fixed audit path and a secret supplied by the runtime. Avoid overlapping scheduled runs. This repository does not install a scheduler, configure WhatsApp, change a live Hermes deployment, or implement backups, rollback, compatibility checks, or public-page smoke checks. Review compatibility and backup coverage when approving a version pair. The local WP-CLI bridge is intended for single-site WordPress and exact releases supported by WP-CLI. Test premium plugins on staging before using live apply.

## Standard MCP adapter contract

Tool inputs and outputs are JSON objects. The client accepts structured MCP output or one JSON text block.

| Tool | Input | Output |
| --- | --- | --- |
| `maintenance_inventory` | `site_id` | `site_id`, `plugins` array with unique `slug`, string `version`, boolean `active`, and offered `target` string or null |
| `maintenance_refresh` | `site_id` | Same `site_id` and `status: "confirmed"` only after refresh finishes |
| `maintenance_update` | `site_id`, `slug`, `from_version`, `target_version`, `active` | Same `site_id` and `slug`, and `status: "completed"` only after completion |

The server must reject mismatched site, version or activation state and preserve activation state during updates. An asynchronous vendor adapter should wait for its action to complete before reporting success. Run only trusted MCP servers. Treat stdio commands and the local configuration as trusted operator inputs.
