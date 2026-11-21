# xCREAS for Home Assistant

[![HACS Action](https://github.com/xCREAS/xcreas-home-assistant/actions/workflows/validate.yml/badge.svg)](https://github.com/xCREAS/xcreas-home-assistant/actions)
![Version](https://img.shields.io/badge/version-1.0.0-blue)

Official xCREAS integration for Home Assistant. Connect your xCREAS air
purifier (V350s) through the xCREAS cloud: each authorized purifier appears
as a **fan** with power, stepless 1–100% speed control and operating presets,
alongside **PM2.5**, **temperature** and **humidity** sensors. Device changes
are pushed into Home Assistant over a dedicated cloud channel — no inbound
ports on your home network and nothing to poll manually.

Authorization uses the OAuth2 authorization-code flow with **PKCE (S256)**
against the xCREAS public client. The integration ships **no client secret**:
you authorize per installation, choose exactly which devices Home Assistant
may see, and can revoke any connection from the xCREAS app at any time.

## Features

- **Fan entity** with power, stepless speed (1–100% mapped onto the device's
  RPM table) and the operating presets `auto`, `sleep`, `eco`.
- **Environment sensors**: PM2.5 (µg/m³), temperature (°C) and humidity (%).
- **Cloud push**: device state, telemetry, presence and connection changes
  arrive as coalesced notices; Home Assistant pulls a fresh snapshot per
  notice (merge-pull) with a low-frequency calibration poll as a safety net.
- **Honest state**: entities never guess. Values converge from device
  reports only — commands are not optimistic, missing fields render as
  `unknown`, and a purifier that is offline renders as `unavailable`.
- **Per-installation authorization**: each Home Assistant instance gets its
  own grant with its own device selection; revoke any connection from the
  xCREAS app (Integrations → Home Assistant) and its access ends immediately.
- **Secure by construction**: public OAuth client + PKCE, no reusable secret
  in the integration package, tokens stored in Home Assistant's own storage.

## Requirements

- Home Assistant **2025.4 or later**. The floor is enforced in
  `manifest.json`: HA Core 2025.4.0 introduced the PKCE-capable OAuth2
  helper this integration uses, so older versions cannot load it. The full
  test suite is verified against HA 2025.4.4, 2025.12.0 and 2026.10.4.
- An xCREAS account with at least one **online, bound, confirmed** V350s.
- A device shared **with you** must have an **accepted** share in the
  xCREAS app before it can be selected.
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
3. **Tick exactly the devices** this Home Assistant instance may see and
   control, then approve. At least one device must be selected; nothing is
   authorized if you cancel.
4. Home Assistant receives the authorization and creates one device card per
   purifier with its fan and sensors.

One Home Assistant config entry = one xCREAS account. See
[Multiple accounts](#multiple-accounts) for how the same purifier behaves
across accounts.

## Entities

| Entity | Values | Notes |
|---|---|---|
| `fan.<device>` | on/off, percentage, preset | Power follows the **reported** power only — an ECO-standby motor stop never fakes "off". 0% turns the purifier off. The displayed percentage shows the device-reported setpoint **when the projection is exact**; otherwise it is hidden rather than guessed. Presets clear when the device reports manual speed. |
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
  fabricated default.
- **Freshness.** Values carry their cloud receive time; the cloud classifies
  staleness against a 5-minute window. Stale values remain real device data
  and are never replaced by guesses.

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
   This stops the Home Assistant side and releases its connection.
2. xCREAS app: Integrations → Home Assistant → delete the connection. This
   revokes the grant; any tokens Home Assistant holds stop working
   immediately.

## Reconnecting and revoked access

If the authorization becomes invalid (revoked in the app, password reset),
the integration detects it — via the push channel, the periodic token
re-validation, or a failed refresh — and starts the **re-authenticate** flow
automatically. Sign in again and re-select the devices; no uninstall needed.

## Multiple accounts

- Each config entry is one xCREAS account; adding the same account twice
  aborts (`already_configured`).
- A purifier can be controlled by **only one** xCREAS connection per Home
  Assistant instance. A second authorization that includes an already-linked
  device aborts with `device_already_linked`. Revoke the holding connection
  in the app first, then connect from the other account.
- Devices newly bound or newly shared to you require a re-authorization —
  the selection is always an explicit decision, never a silent expansion.

## Security and privacy

- The integration package carries **no client secret**: it is a public
  OAuth2 client using the authorization-code flow with PKCE (S256).
- Only the devices you explicitly selected are ever visible to Home
  Assistant; unselected devices send nothing.
- Tokens live in Home Assistant's own storage and are refreshed
  automatically; revoking the connection in the xCREAS app invalidates them
  server-side.
- The connection is outbound from your Home Assistant to the xCREAS cloud —
  no port forwarding, no inbound firewall rules.

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
| "Re-authenticate" needed | The authorization was revoked or expired. Complete the flow; your entities are preserved. |
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
