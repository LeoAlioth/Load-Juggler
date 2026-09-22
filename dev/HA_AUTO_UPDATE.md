# Auto-downloading a new build into Home Assistant

Every push to `dev` / `dev-*` builds a tagged pre-release and
publishes it to both Gitea and GitHub. HACS reads the **GitHub** repository, but
it only notices a new version when its own periodic check happens to run - so a
freshly pushed build can sit unnoticed for hours.

The release workflows close that gap: their last step POSTs the tag it just
published to a Home Assistant webhook, and an automation there tells HACS to
download that exact version. HA is **not** restarted automatically - a custom
integration's code is only loaded on restart, and restarting unattended would
swap out live EVSE and hot water tank control mid-operation. You get a
notification instead and restart when it suits you.

## The update entity

HACS's update entity for this repository is **`update.dynamic_ocpp_evse_update`**
on every instance that installed it before 2026-09-16 - the entity id was formed
from the GitHub repository's name at the time (`Dynamic_OCPP_EVSE`) and entity
ids do not follow renames, so it survives the repository's rename to
`Load-Juggler`; the entity's display NAME does follow it. A fresh install after
the rename gets `update.load_juggler_update` instead. List the real ids with
this in Developer Tools → Template:

```jinja
{{ states.update | map(attribute='entity_id') | list }}
```

## The automation

One automation per instance serves every Load Juggler-family repository. The
webhook payload carries the repository, and the automation maps it to that
instance's HACS update entity - the mapping lives HERE, not in the workflow,
because entity ids depend on when HACS first saw the repository (an install
from before the 2026-09-16 rename has `update.dynamic_ocpp_evse_update`, a
fresh one `update.load_juggler_update`). Check yours with the template above
and edit the `entities` map to match.

Add this in Home Assistant (Settings → Automations → new → Edit in YAML), or
paste it into `automations.yaml`:

```yaml
alias: Load Juggler family - download pushed build
description: Downloads the build a Gitea release workflow just published, for whichever repository sent it.
triggers:
  - trigger: webhook
    webhook_id: lj-family-build             # change this - it is the shared secret
    allowed_methods: [POST]
    local_only: true                        # runner and HA are on the same network
variables:
  entities:
    LeoAlioth/Load-Juggler: update.dynamic_ocpp_evse_update
    LeoAlioth/Load-Insights: update.load_insights_update
  target: "{{ entities.get(trigger.json.repository) }}"
conditions:
  - condition: template
    value_template: "{{ target is not none and target != 'None' }}"
actions:
  - action: update.install
    target:
      entity_id: "{{ target }}"
    data:
      version: "{{ trigger.json.version }}"
  - action: persistent_notification.create
    data:
      title: "{{ trigger.json.repository.split('/')[1] }} {{ trigger.json.version }} downloaded"
      message: Restart Home Assistant to load it.
      notification_id: "{{ trigger.json.repository.split('/')[1] | lower }}_restart_pending"
mode: queued
```

A payload naming a repository the map does not know is ignored, so adding a
third integration later is one line here and nothing else.

Then add the matching secret in Gitea. Since one webhook now serves every
repository, put it at USER level (avatar → Settings → Actions → Secrets) so all
of them read it, and delete any repository-level copy:

| Secret | Value |
| ------ | ----- |
| `HA_WEBHOOK_URLS` | One webhook URL per line - see below |
| `HA_WEBHOOK_URL` | Single-instance alternative, still honoured |

Use each instance's IP rather than a hostname - `act_runner` in a container often
can't resolve local hostnames. If neither secret is set the workflow step logs that
it is skipping and moves on, so this is entirely opt-in.

### Several instances

`HA_WEBHOOK_URLS` is split on whitespace, so a multi-line secret notifies every
system from one release. Lines beginning with `#` are ignored:

```
# on-grid test system
http://192.168.1.20:8123/api/webhook/load-juggler-build-ongrid
# off-grid test system
http://192.168.5.20:8123/api/webhook/load-juggler-build-offgrid
```

Give each instance its **own** `webhook_id` and use it in that instance's
automation. They are the only credential on the endpoint, so a shared one means
either system can be triggered by anyone who learns it - and distinct IDs make the
workflow log say which system answered.

Each URL is notified independently: one unreachable instance logs a failure line
and the others still get their build. The step never fails the release.

## Why it passes an explicit version

`update.install` accepts a `version` (a tag, a public branch, or a commit SHA),
and passing one means HACS fetches that release directly instead of relying on
having already detected an update. That is what makes this independent of HACS's
check interval.

It has to be a **release tag**, not a branch: `hacs.json` sets
`zip_release: true`, so HACS downloads the `dynamic-ocpp-evse.zip` asset
attached to the release, and a branch has no such asset.

## Notes

- **Pre-release switch:** dev builds are published with `prerelease: true`.
  HACS gives each repository a pre-release switch entity that decides whether
  pre-release tags are considered at all. Turn it on for this repository.
- **No manifest bump needed per build:** the workflows derive the tag from
  `manifest.json`'s version plus a UTC timestamp, so every push produces a new,
  strictly increasing tag. `manifest.json` only changes when the base version does.
- **Faster loop without HACS:** for tight iteration, skip all of this - `rsync`
  `custom_components/dynamic_ocpp_evse/` into the HA config directory and restart.
  No tag, no round-trip through GitHub.
