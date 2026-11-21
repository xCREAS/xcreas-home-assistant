# xCREAS for Home Assistant

Connect xCREAS air purifiers (V350s) to Home Assistant through the xCREAS
cloud. Each authorized purifier appears as a **fan** (power / stepless speed /
presets) plus **PM2.5 / temperature / humidity** sensors, and device changes
push into HA through a dedicated cloud channel — no inbound ports on your
home network.

This is the **beta (手动安装)** release: install by copying the folder, no
HACS store entry yet. The cloud-side contract lives in the xcreas-api repo:
`docs/ha-integration.md`.

## Requirements

- Home Assistant **2025.4 or later** (declared in `manifest.json`). The
  floor is the 2025.4.0 introduction of `LocalOAuth2ImplementationWithPkce`
  in HA Core — below that the integration cannot load. Verified by running
  the full test suite on HA 2025.4.4, 2025.12.0 and 2026.10.4 (HA ≤ 2025.3
  is unsupported, not merely untested).
- An xCREAS account with at least one **online, bound, confirmed** V350s.
- Devices shared with you must have an **accepted** share in the xCREAS app.
- HACS users need HACS ≥ 1.30 for the custom-repository flow below.

## Installation

### Option A — HACS (custom repository, recommended for beta)

1. HACS → ⋮ (top right) → **Custom repositories**.
2. Paste this repository's GitHub URL, set **Category: Integration**, Add.
3. The xCREAS card appears under HACS → Integrations → **Download**.
4. Restart Home Assistant.
5. Continue with the setup flow below.

Updates: when a new release is tagged, HACS offers the update — one click +
restart. No file copying.

### Option B — GitHub release zip (no HACS)

1. Open the repo's **Releases** page and download the latest release's
   **Source code (zip)**.
2. From the archive, copy the `custom_components/xcreas/` folder into your
   HA config's `config/custom_components/xcreas/`.
3. Restart Home Assistant.

### Option C — clone/copy (developer)

Copy `custom_components/xcreas/` into `config/custom_components/xcreas/` and
restart.

### Setup flow (all options)

1. **Settings → Devices & Services → Add Integration → xCREAS**.
2. Sign in to xCREAS in the browser window and **tick the devices** this HA
   instance may control, then approve.
3. HA receives the authorization and creates one device per purifier.

To update: Option A users take HACS's update prompt; Options B/C replace the
folder contents and restart. To uninstall: remove the integration in HA
(Settings → Devices & Services → xCREAS → Delete) — this stops HA's
connection — and revoke the connection in the xCREAS app
(**Integrations → Home Assistant → 解除连接**) to invalidate its tokens.

## Multi-account rules

- One HA config entry = one xCREAS account. Adding the same account twice
  aborts (`already_configured`).
- A purifier can be controlled by **only one** xCREAS entry per HA instance;
  the second add aborts with `device_already_linked`. Revoke the holding
  connection in the app first, then re-add from the other account.

## What the entities do

| Entity | Notes |
|---|---|
| `fan.*` | Power judges the reported power only (ECO standby never fakes "off"). Speed 1–100% maps onto the cloud's RPM table; the displayed percentage shows the device-reported setpoint when it is exact. Presets: `auto`, `sleep`, `eco`. 0% turns the purifier off. |
| `sensor.*_pm25` | µg/m³, measurement. |
| `sensor.*_temperature` | °C, measurement. |
| `sensor.*_humidity` | %, measurement (converted once on the cloud side). |

Entities show `unavailable` when the purifier or the cloud connection is
gone; a field the device has not reported (yet) shows `unknown`. Values are
never fabricated: states converge from device reports only.

## Reconnect / revoked access

If the xCREAS authorization is revoked (app-side delete, password reset) the
integration starts the **re-authenticate** flow automatically. The cloud
pushes the revocation to open connections; a missed push is bounded by the
connection's periodic token re-validation (≤5 min).

## Automation example (V07-style test)

```yaml
automation:
  - alias: "V350s PM2.5 threshold demo"
    trigger:
      - platform: numeric_state
        entity_id: sensor.<device>_pm25
        above: 15
        for: "00:00:05"
    action:
      - service: fan.turn_on
        target:
          entity_id: fan.<device>
```

The threshold here is only a repeatable test rule — it is not a health
recommendation.

## HACS inclusion checklist (repo housekeeping)

The `hacs/action` validation requires three repository-level things that
live OUTSIDE the code:

1. **Description** — GitHub → Settings → General → Description (or
   `gh repo edit --description "..."`).
2. **Topics** — at least one topic on the repo (recommended:
   `home-assistant`, `hacs`, `integration`, `air-purifier`), via the
   About ⚙️ dialog or `gh repo edit --add-topic home-assistant`.
3. **Brand assets** — `custom_components/xcreas/brand/icon.png`
   (+ `icon@2x.png`, `logo.png`) is committed here, which satisfies the
   brands check locally. When HACS default-store inclusion is pursued, a
   PR to the `home-assistant/brands` repository
   (`custom_integrations/xcreas/…`) is the canonical long-term home.

None of these are required for the BETA custom-repository distribution —
they gate the future default-store listing only.

## Support

- Cloud contract: `xcreas-api` → `docs/ha-integration.md`.
- Issues: https://github.com/xiezichao/xcreas-home-assistant/issues
- The integration never stores a client secret: the OAuth client is a
  public client with PKCE S256, and tokens live in HA's own storage.
