# OpenRocket to JSBSim

Basic converter for turning an OpenRocket `.ork` design into starter JSBSim files.

This project is intentionally a first-pass engineering tool. It uses the current
VADL `summer_subscale_2026` JSBSim layout as the baseline for what needs to be
filled out.

## What It Does Now

- Opens `.ork` files directly. An `.ork` is a ZIP file containing OpenRocket XML.
- Parses the rocket component tree.
- Extracts:
  - rocket name/comment
  - body/nose/transition components
  - fin point geometry
  - RACS/freeform fin geometry
  - mass overrides
  - parachute diameter/Cd/deployment event
  - motor selections and default motor config
  - launch rail length/angle/direction
  - wind speed/turbulence/direction
- Generates a starter JSBSim aircraft folder:
  - `aircraft/<rocket>/<rocket>.xml`
  - `aircraft/<rocket>/Engines/<motor>_engine.xml`
  - `aircraft/<rocket>/Engines/<motor>_nozzle.xml`
  - `aircraft/<rocket>/Systems/README.md`
- Generates starter C++ files:
  - `rocket_sim_<rocket>.cpp`
  - `hitl_sim_<rocket>.cpp`
- Generates `conversion_report.txt`.
- Can install directly into a JSBSim repo with `--install-jsbsim`.
- Can also build the generated JSBSim targets with `--build-jsbsim`.
- Writes XML comments describing whether each generated section came from the `.ork`, OpenRocket motor database, or the VADL summer-subscale JSBSim baseline.

## What It Does Not Fully Solve Yet

This is not a perfect OpenRocket-to-JSBSim physics translation yet.

Current limitations:

- Motor thrust tables now come from a real OpenRocket motor database match by default. The converter searches common `motors.db` locations, honors `OPENROCKET_MOTOR_DB`, accepts `--motor-db`, and can download OpenRocket published `motors.db.gz` into `motor_data/`.
- The converter refuses to invent thrust unless `--allow-placeholder-thrust` is explicitly provided for rough testing.
- OpenRocket `.ork` files often store motor identity/digest, not the full thrust curve, so the motor database is the source of truth for thrust samples.
- CG/CP are currently estimated unless pulled from OpenRocket or manually supplied.
- JSBSim aerodynamic coefficients are baseline approximations.
- Active RACS/ACS control behavior is generated as JSBSim properties and placeholder roll moments, not inferred automatically from OpenRocket.
- The generated C++ files are starter scaffolds, not full copies of the VADL HIL loop.

## Recommended Future Pipeline

Best long-term flow:

```text
OpenRocket .ork
   -> parse geometry/mass/recovery/motor config
   -> import real motor thrust curve from RASP/RockSim/ThrustCurve/OpenRocket DB
   -> compute reference area, fin area, fin arm, CG, CP
   -> generate JSBSim aircraft XML
   -> generate JSBSim engine/nozzle XML
   -> optionally generate CMake target + HIL runner
   -> compare JSBSim output against OpenRocket simulation CSV
```


## Easy Workflow

Use the wrapper script from WSL:

```bash
cd "/mnt/c/Users/ramirm9/OneDrive - Vanderbilt/Documents/GitHub/open rocket to jsbsim"
./convert_ork.sh "/mnt/c/Users/ramirm9/Downloads/CURRENT_Subscale.ork" --run
```

That one command converts the `.ork`, installs it into `~/jsbsim-rocket-hitl`, builds the JSBSim targets, and runs the standalone simulation.

To choose your own aircraft name:

```bash
./convert_ork.sh "/mnt/c/Users/ramirm9/Downloads/My Rocket.ork" my_rocket_convert --run
```

To build only and not run:

```bash
./convert_ork.sh "/mnt/c/Users/ramirm9/Downloads/My Rocket.ork" my_rocket_convert
```

## Usage

From this folder:

```powershell
python .\openrocket_to_jsbsim.py "C:\Users\ramirm9\Downloads\26 Summer Subscale.ork" --name summer_subscale_2026 --output .\generated
```

Output will appear in:

```text
generated/
```

## Why Not Just Use OBJ/RockSim/RASAero Export?

- `.ork` is the best primary source for the design tree.
- `OBJ` is useful for visuals only, not physics.
- `RockSim .rkt` may be useful as a secondary compatibility format but can lose OpenRocket-specific details.
- `RASAero .CDX1` may help aerodynamic validation, but it is not the cleanest source for JSBSim generation.
- OpenRocket simulation CSV is very useful for validation, not model generation.

## RACS/ACS Behavior

OpenRocket stores the fin geometry, but not the full control mechanism JSBSim needs.

The converter can generate JSBSim properties like:

```xml
<property value="0">fcs/racs_fin_1_pos_rad</property>
```

and moment functions like:

```text
roll moment = qbar * fin_area * rocket_radius * effectiveness * sin(fin_angle)
```

That means JSBSim can run the RACS behavior, but the converter must create those
mechanics explicitly. OpenRocket does not provide them directly.

