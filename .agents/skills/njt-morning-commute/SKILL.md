---
name: njt-morning-commute
description: Use for live commute decisions from RVR via Secaucus Junction to New York Penn, including departure windows, train delays, GPS positions, and station-status checks.
---

# NJ TRANSIT morning commute

Use this workflow when the user asks which train to take or when to leave for the RVR → Secaucus Junction → New York Penn commute. Keep the answer focused on the requested route. Include Hoboken only when the user asks about it.

## Route timing and station levels

- For this commute, allow about 7 minutes to drive from RVR to Secaucus and 3 minutes to reach the platform. Preserve a 2-minute boarding buffer: plan to leave 12 minutes before the estimated Secaucus departure.
- Use America/New_York time and label displayed times ET. Reply in the user's language, usually Chinese.
- Secaucus has distinct platform entries in RailData. New York Penn-bound trains use **Secaucus Upper Lvl**; Hoboken-bound trains use **Secaucus Lower Lvl**. Resolve the station code from `getStationList` by platform name. Current observed codes are `SE` (Upper) and `TS` (Lower); do not hardcode these values. `SC` is Secaucus Concourse and is not a train platform board.
- The current CLI is Penn-focused and should select the Upper Level. If adding a Hoboken query, pass `destination="Hoboken"` to `find_secaucus_code` so the resolver selects the Lower Level.

## Get a fresh live snapshot

For a current commute decision, refresh the production RailData feed for that request. Use the project CLI from the repository root. The no-argument form is the focused RVR → Secaucus → Penn board:

```sh
uv run python njt_commute.py
```

For a station-to-station query, pass the origin and destination names directly:

```sh
uv run python njt_commute.py "Secaucus Junction" "New York Penn"
uv run python njt_commute.py "Secaucus Junction" "Hoboken"
```

This command resolves the origin code from the live station list, queries the matching station board, and fetches live vehicle positions in parallel. For Secaucus, the destination selects Upper Level for New York Penn and Lower Level for Hoboken. It prints a Markdown table with departure time, the board's delay field, origin stop status, GPS coordinates and age, straight-line distance to Secaucus, and remaining stops when `NEXT_STOP` matches the train's ordered stop list. The remaining-stop count includes the next stop and destination. If `NEXT_STOP` is unavailable, unmatched, or stale, mark the count unavailable or qualify it with the vehicle timestamp. Do not infer destination-specific delay from the train's current delay; omit that column unless the user asks and the API provides a separate value. Keep planned departure (DEP_TIME / SCHED_DEP_DATE) separate from the delay-adjusted estimate (SCHED_DEP_DATE + SEC_LATE). Their difference normally represents delay, not a data conflict. Show OnTime as a separate categorical field. Use the upcoming planned time conservatively for boarding advice, clearly distinguishing it from the displayed estimate. Select GPS coordinates and timestamp from one complete sample. Use the vehicle timestamp for NEXT_STOP freshness and omit remaining counts over 120 seconds old or contradicted by DEPARTED=YES. Show counts to both origin and destination. Only add the RVR 12-minute leave-by calculation when the origin is Secaucus Junction; for other origin stations, show train departure choices without assuming the user's access time.

The CLI uses the production API by default and reuses its local token cache. Use this existing authentication and API flow instead of rebuilding it. Do not use test-feed data as live data or substitute web search for the current API snapshot. If the live call fails or access is unavailable, say that current data could not be confirmed; do not rely on an older run. Follow the active environment's network and approval controls.

Live production lookups are read-only. User authorization to check current feeds does not authorize ticket purchases, bookings, account changes, or other write actions. Never display, copy, or ask the user to paste `.env` credentials or `.njt_token.json` contents.

The station board is the primary source. If it returns no usable Penn-bound rows, first confirm the resolver selected Secaucus Upper Lvl (`SE` in the current feed), then use the CLI's fallback built from active vehicles and individual train stop lists. A fallback estimate combines the Secaucus stop's `DEP_TIME` with that vehicle's current `SEC_LATE`; label it as a fallback estimate, not as a station-board departure or a measured Secaucus delay.

## Interpret delay, GPS, and stop data

- For direct station-board rows, report `SEC_LATE` as the delay shown on that Secaucus board snapshot. Include the Secaucus stop's `DEP_TIME`, `STOP_STATUS`, and `DEPARTED` when available.
- For fallback rows, call `SEC_LATE` the vehicle's current reported delay. Keep it separate from stop-level status; if the values disagree, show both and use the earlier departure as the conservative boarding assumption.
- At New York Penn, report the stop-list `TIME` and `STOP_STATUS` when available. Do not convert a vehicle delay into an asserted Penn-specific delay or ETA unless the API provides a separate Penn estimate.
- Treat service messages as advisories, not numeric train delays. If a stop's time or status is missing, say so.
- Treat GPS as the train's last reported position, not a speed-based arrival prediction. Give its timestamp and straight-line distance to Secaucus when available. Never extrapolate an arrival time from distance alone.
- Calculate the age of each vehicle `LAST_MODIFIED` against the snapshot time: up to 30 seconds is recent, 31–60 seconds is aging, over 60 seconds is stale, and over 120 seconds is very stale. These are practical caution thresholds, not NJ TRANSIT freshness guarantees. Do not base a confident recommendation on stale position or delay data.

## Choose when to leave

1. Capture the current ET time and use the user's stated availability window. If none is given, assume they can leave now.
2. Consider upcoming, non-cancelled Penn-bound trains that have not departed Secaucus. Prefer the direct board's departure estimate. If using the stop-list fallback, use `DEP_TIME + SEC_LATE` and label it as an estimate.
3. For an estimated Secaucus departure `D`, the safe leave-by time is `D − 12 minutes`. If the user can leave only between `D − 12` and `D − 10`, mark the train tight and not assured. With fewer than 10 minutes before `D`, mark it unreachable from RVR.
4. Do not recommend leaving before the start of the user's availability window. Prefer the earliest train reachable with the full buffer; if none fits, show the nearest tight option and the next safe train.
5. If data is missing, stale, or inconsistent, make that uncertainty visible. Do not let a nearby GPS point override a departed/status field or prove that boarding is still possible.

## Response shape

Lead with the suggested leave time and train when the feed supports a recommendation. State the 10-minute drive-and-platform estimate and 2-minute boarding buffer. For station-to-station queries, preserve the CLI's Markdown table and briefly explain its columns. State the snapshot time, surface relevant advisories, and distinguish direct-board values from fallback estimates. For New York Penn, do not add an arrival-delay column based on `SEC_LATE`; show a Penn stop time/status only when it helps and label it as the feed value.
