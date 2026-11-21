# xCREAS for Home Assistant

[![HACS Action](https://github.com/xCREAS/xcreas-home-assistant/actions/workflows/validate.yml/badge.svg)](https://github.com/xCREAS/xcreas-home-assistant/actions)
![Version](https://img.shields.io/badge/version-1.1.0-blue)

Official xCREAS integration for Home Assistant. Connect your xCREAS air
purifier (V350s) through the xCREAS cloud: each purifier appears as a **fan**
with power, stepless 1–100% speed control and operating presets, alongside
**PM2.5**, **temperature** and **humidity** sensors. Device changes are
pushed into Home Assistant over a dedicated cloud channel — no inbound ports
on your home network and nothing to poll manually.

Authorization is **account-level** with the OAuth2 authorization-code flow +
**PKCE (S256)** against the xCREAS public client: you confirm ONCE, and Home
Assistant follows your account from then on. Devices you bind or accept to
share appear automatically; devices you unbind, lose the share for, or that
leave the supported fleet are removed automatically — no re-authorization,
no manual re-discovery, no restart. A device that is merely offline stays
in place and renders as `unavailable`. You can revoke any connection from
the xCREAS app at any time.

## Features

- **Account sync**: one confirmation covers every supported device your
  account can access **now and in the future** — including accepted shares.
  Bind a purifier in the xCREAS app and it appears in Home Assistant without
  any action here; unbind it and it disappears on its own.
- **Fan entity** with power, stepless speed (1–100% mapped onto the device's
  RPM table) and the operating presets `auto`, `sleep`, `eco`.
- **Manual gear selector** for the purifier's three fixed speeds. Open the
  xCREAS device page and choose Gear 1, 2, or 3 under **Manual gear**. The
  selection follows the device report. The cloud API must advertise gear
  control when the integration loads. Reload the entry after a cloud upgrade
  if the selector is missing.
- **Environment sensors**: PM2.5 (µg/m³), temperature (°C) and humidity (%).
- **Cloud push**: device state, telemetry, presence and connection changes
  arrive as coalesced notices; Home Assistant pulls a fresh snapshot per
  notice (merge-pull) with a low-frequency calibration poll as a safety net
  — the sync also works when the push connection is temporarily down and
  recovers by itself.
- **Zero devices is fine**: you can connect an account that has no eligible
  devices yet; everything you bind later shows up automatically.
- **Honest state**: entities never guess. Values converge from device
  reports only — commands are not optimistic, missing fields render as
  `unknown`, and a purifier that is offline renders as `unavailable`.
- **Per-installation authorization**: each Home Assistant instance gets its
  own grant for the account; revoke any connection from the xCREAS app
  (Integrations → Home Assistant) and its access ends immediately —
  including live connections and tokens.
- **Secure by construction**: public OAuth client + PKCE, no reusable secret
  in the integration package, tokens stored in Home Assistant's own storage.

## Requirements

- Home Assistant **2025.4 or later**. The floor is enforced by `hacs.json`
  for HACS installs (HA Core 2025.4.0 introduced the PKCE-capable OAuth2
  helper this integration uses); manual installs must check the version
  themselves. The full test suite is verified against HA 2025.4.4,
  2025.12.0 and 2026.10.4.
- An xCREAS account. **No device is required to connect** — zero-device
  accounts complete the setup and sync devices as they appear. Devices must
  be online-bound and confirmed in the xCREAS app to become eligible.
- A device shared **with you** must have an **accepted** share in the
  xCREAS app before it can appear here.
- For the HACS route: HACS 1.30 or later.

## Installation

### Option A — HACS (custom repository, recommended)

1. HACS → ⋮ (top-right) → **Custom repositories**.
2. Paste `https://github.com/xCREAS/xcreas-home-assistant`, set
   **Category: Integration**, and select **Add**.
3. The xCREAS card appears under HACS → Integrations → **Download**.
4. Restart Home Assistant.
5. Continue with [First-time setup](#first-time-setup).

Updates arrive through HACS: when a new release is published you get the
update prompt — one click, then restart Home Assistant.

### Option B — Manual install from a GitHub release zip

1. Open the repository's **Releases** page and download the latest release's
   **Source code (zip)**.
2. From the archive, copy the `custom_components/xcreas/` folder into your
   Home Assistant configuration directory: `config/custom_components/xcreas/`.
3. Restart Home Assistant.
4. Continue with [First-time setup](#first-time-setup).

### Option C — From source (developers)

Clone this repository and copy `custom_components/xcreas/` into
`config/custom_components/xcreas/`, then restart Home Assistant.

## First-time setup

1. **Settings → Devices & Services → Add Integration → xCREAS**.
2. A browser window opens the xCREAS authorization page. Sign in with your
   xCREAS account.
3. **Confirm the authorization once** and return to Home Assistant. The
   page shows your account and what the grant covers — there are no device
   checkboxes. Works with zero devices: whatever your account can access
   now (or comes to access later) syncs automatically.

One Home Assistant config entry = one xCREAS account. See
[Multiple accounts](#multiple-accounts) for how the same purifier behaves
across accounts.

## Entities

| Entity | Values | Notes |
|---|---|---|
| `fan.<device>` | on/off, percentage, preset | Power follows the **reported** power only — an ECO-standby motor stop never fakes "off". 0% turns the purifier off. The displayed percentage shows the device-reported setpoint **when the projection is exact**; otherwise it is hidden rather than guessed. Presets clear when the device reports manual speed. |
| `select.<device>_manual_gear` | `gear_1`, `gear_2`, `gear_3` | Fixed manual gear. For automations, call `select.select_option` with one of these option keys. |
| `sensor.<device> pm2.5` | µg/m³ | Device class `pm25`, measurement. |
| `sensor.<device> temperature` | °C | Device class `temperature`, measurement. |
| `sensor.<device> humidity` | % | Converted exactly once on the cloud side; Home Assistant never rescales. |

Behavior guarantees:

- **No optimistic writes.** A command is confirmed against the device before
  state changes; entities converge from device reports, and a late or lost
  confirmation never fakes success.
- **`unavailable` vs `unknown`.** A purifier that is offline, or a cloud
  connection that is down, renders its entities `unavailable`. A field the
  device has not reported (yet) renders `unknown` — never zero, never a
  fabricated default. An offline purifier is NEVER removed from Home
  Assistant — only losing access (unbind / share withdrawn / frozen) removes it.
- **Freshness.** Values carry their cloud receive time; the cloud classifies
  staleness against a 5-minute window. Stale values remain real device data
  and are never replaced by guesses.
- **Stable identities.** Entities are keyed by the cloud's device id.
  Renames, restarts, re-authentication and account switches (where the
  source account changes) keep the same entity IDs and history.

## Automations

Everything is an ordinary Home Assistant entity, so any automation works.
A repeatable threshold example:

```yaml
automation:
  - alias: "V350s PM2.5 threshold"
    trigger:
      - platform: numeric_state
        entity_id: sensor.<device>_pm2_5
        above: 15
        for: "00:00:05"
    action:
      - service: fan.turn_on
        target:
          entity_id: fan.<device>
```

The threshold above is a repeatable test rule, not a health recommendation.

## Updating

- **HACS (Option A)**: take the update prompt when a new release is
  published, then restart Home Assistant.
- **Manual (Options B/C)**: replace the contents of
  `config/custom_components/xcreas/` with the new release's folder, then
  restart. Your authorization and entities survive updates — entity IDs and
  the stored authorization are stable across versions.

## Uninstalling

1. Home Assistant: Settings → Devices & Services → xCREAS → **Delete**.
   This stops the Home Assistant side, releases its connection and removes
   the entities this integration created.
2. xCREAS app: Integrations → Home Assistant → delete the connection. This
   revokes the grant; any tokens Home Assistant holds stop working
   immediately.

## Reconnecting and revoked access

If the authorization becomes invalid (revoked in the app, password reset),
the integration detects it — via the push channel, the periodic token
re-validation, or a failed refresh — and starts the **re-authenticate** flow
automatically. Sign in again with the SAME xCREAS account and confirm once;
the original connection and your entities are preserved. Signing in with a
different account is refused (`account_mismatch`) — remove the connection
first if you truly want to hand it to another account.

## Multiple accounts

- Each config entry is one xCREAS account; adding the same account again
  refreshes the existing entry (`already_configured`), it never duplicates.
- A purifier shared into two accounts appears **once** per Home Assistant
  instance: the integration registers one set of entities per device and
  keeps the entity identity even when the controlling account switches.
  If the current source account loses access, the other account's entry
  takes the device over automatically.
- Devices appear and disappear following your account's live access —
  a new binding or an accepted share shows up by itself, a withdrawal or
  unbind removes itself. There is no per-device selection to update.

## Security and privacy

- The integration package carries **no client secret**: it is a public
  OAuth2 client using the authorization-code flow with PKCE (S256), with
  the explicit account scope `homeassistant:account`.
- The grant covers the devices your account can access through the xCREAS
  cloud, re-checked against live permissions on every read — a device you
  lose access to stops being visible and controllable immediately, even
  before Home Assistant refreshes.
- Tokens live in Home Assistant's own storage and are refreshed
  automatically; revoking the connection in the xCREAS app invalidates them
  server-side.
- The connection is outbound from your Home Assistant to the xCREAS cloud —
  no port forwarding, no inbound firewall rules. The default OAuth callback
  rides the My Home Assistant redirect service (`my.home-assistant.io`);
  deployments that connect to instances directly must register the instance
  callback URI on the xCREAS cloud side.

## Troubleshooting

Enable debug logging for this integration, then reproduce the issue:

```yaml
logger:
  logs:
    custom_components.xcreas: debug
```

| Symptom | Meaning |
|---|---|
| Entities `unavailable` | The purifier is offline, or the cloud connection is down. Check the device and your connection; it recovers on its own. |
| A sensor shows `unknown` | The device has not reported that field yet. It fills in on the next device report — never fabricated. |
| "Re-authenticate" needed | The authorization was revoked or expired. Complete the flow with the SAME account; your entities are preserved. |
| Device list stale (bound device missing / removed device still there) | The inventory refreshes on push notices, on reconnect and at least every 5 minutes. If it lags: open the integration's diagnostics log for snapshot errors, verify the device's binding/share state in the xCREAS app, then reload the integration entry (Settings → Devices & Services → xCREAS → ⋮ → Reload). |
| Commands "work but state is slow" | State converges from device reports pushed to Home Assistant. If it takes minutes, check the logs for push-connection warnings. |
| `ws handshake rejected: HTTP 4xx/5xx` in logs | The push connection was refused — include these lines when opening an issue. |

## Repository notes (HACS inclusion)

The `hacs/action` validation checks three repository-level items outside the
code: a repository **description**, at least one **topic**, and **brand
assets** — satisfied locally by `custom_components/xcreas/brand/`
(`icon.png`, `icon@2x.png`, `logo.png`). When HACS default-store inclusion
is pursued, a PR to the `home-assistant/brands` repository
(`custom_integrations/xcreas/…`) is the canonical long-term home for the
assets. None of this affects installation or operation.

## Support

- Cloud-side contract: `xcreas-api` repository, `docs/ha-integration.md`.
- Issues: <https://github.com/xCREAS/xcreas-home-assistant/issues>
- Releases: <https://github.com/xCREAS/xcreas-home-assistant/releases>

## License

[MIT](LICENSE)
