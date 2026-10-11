# NJ TRANSIT Secaucus commute board

A small, read-only CLI for the Secaucus Junction → New York Penn commute. It shows upcoming Penn-bound departures, headways, live coordinates, straight-line distance from each reported train position to Secaucus Junction, and delay/status details at the Secaucus and New York Penn stops when the feed provides them. It calculates when to leave home using 7 minutes of driving, 3 minutes to the platform, and a 2-minute boarding buffer. It does not show Hoboken service.

## Set up

1. Install [uv](https://docs.astral.sh/uv/) and copy `.env.example` to `.env`. Add your NJ TRANSIT API username and password to `.env`. The script accepts `NJT_USERNAME` / `NJT_PASSWORD`; it also recognizes common `NJT_API_*`, `NJTRANSIT_*`, and `API_*` names, plus `USERNAME` / `ACCOUNT` and `PASSWORD` inside `.env`.
2. Run the CLI from this directory:

   ```sh
   uv run python njt_commute.py
   ```

   If the `.env` lives elsewhere, set `NJT_ENV_FILE=/path/to/.env` before running.

The project requires Python 3.10 or newer, uses only the Python standard library, and defaults to production at `https://raildata.njtransit.com/api/TrainData`. For the test environment, set `NJT_API_BASE=https://testraildata.njtransit.com/api/TrainData`. The RailData token is requested only when no local token is cached or the API rejects the cached token. The cache is stored in `.njt_token.json` with owner-only file permissions where supported; neither the token nor the credentials are printed.

## Codex skill

The reusable commute workflow is in `.agents/skills/njt-morning-commute/SKILL.md`. Codex discovers repository skills from `.agents/skills/`; invoke it explicitly with `$njt-morning-commute` when needed. It includes the Secaucus Upper Level → New York Penn and Lower Level → Hoboken mapping, along with the live-feed and departure-planning steps.

## API flow and useful operations

- `POST /TrainData/getToken` — multipart fields `username`, `password`; the documented response has `Authenticated` and `UserToken`.
- `POST /TrainData/getStationList` — multipart `token`; Secaucus has separate concourse and platform entries. Resolve the platform from the destination: New York Penn uses `Secaucus Upper Lvl` (currently `SE`), and Hoboken uses `Secaucus Lower Lvl` (currently `TS`). Do not use `Secaucus Concourse` (`SC`) as a train platform; it returned an empty board in the production check. The codes come from the live station list and should be resolved by name rather than hardcoded.
- `POST /TrainData/getTrainSchedule` — multipart `token`, `station`; the response includes departures, GPS coordinates/time, status/delay/track fields, stop times/status, inline messages, and station messages.
- `POST /TrainData/getStationMSG` — station/line messages when a separate message lookup is needed.
- `POST /TrainData/getVehicleData` — train IDs, directions, track circuits, next stops, coordinates, update times, and seconds late for active trains.
- `POST /TrainData/getStationSchedule` — station timetable with an `NJTOnly` filter; use this for timetable data, not as the live board.

The station board is the primary source for departure recommendations. Its `SEC_LATE` value is the train's reported delay for the Secaucus board snapshot. Stop rows can also provide each station's feed time and `STOP_STATUS`; these are shown for Secaucus Junction and New York Penn when available. The documented stop schema does not provide a separate delay-in-seconds value for each stop, so a Penn stop status/time is not presented as an exact Penn delay in minutes. If the board returns no usable Penn-bound rows, the CLI falls back to active vehicles' individual stop lists and combines each Secaucus `DEP_TIME` with current vehicle delay seconds. In that fallback, the delay belongs to the vehicle snapshot and is only an input to the Secaucus departure estimate, not a station-specific Secaucus delay. The fallback is labeled as an estimate; it also shows current coordinates and their update times.

## Security and use notes

- `.env`, other local environment files, and `.njt_token.json` are ignored by Git. Never commit or paste credentials or token contents.
- The portal says tokens last for hours and token issuance is rate-limited, so this tool reuses a cached token and renews only after an unauthorized response.
- The NJ TRANSIT developer terms describe the feeds as provided as-is. Treat this display as a convenience and check NJ TRANSIT's live advisories before departing.

## Documentation

- [NJ TRANSIT Developer Portal](https://developer.njtransit.com/)
- [RailData API catalog documentation](https://developer.njtransit.com/registration/appdoc?app=RailData)
- [RailData test Swagger (v2)](https://testraildata.njtransit.com/swagger/index.html)
- [RailData v2 schema](https://testraildata.njtransit.com/swagger/RailData/swagger.json)
- [GTFS-RAIL API catalog documentation](https://developer.njtransit.com/registration/appdoc?app=GTFS-RAIL)
- [NJ TRANSIT developer terms](https://developer.njtransit.com/terms/)
