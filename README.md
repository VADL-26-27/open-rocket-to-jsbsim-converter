# OpenRocket to JSBSim Converter

This tool converts an OpenRocket `.ork` rocket design into starter JSBSim rocket simulation files. It is meant to make the first JSBSim model generation step faster, repeatable, and less error-prone for VADL rocket work.

The converter does not magically create a perfect flight model. It builds a JSBSim-ready starting point from real OpenRocket geometry, mass, recovery, launch, wind, and motor data, then installs/builds it inside the VADL JSBSim repo when requested.
## Project Status

This project is still in progress. It is useful as an engineering starting point, but it is not finished and should not be treated as a fully validated OpenRocket-to-JSBSim physics converter yet.

The current goal is to automate the repetitive file-generation work while preserving enough comments and structure for engineers to tune the generated JSBSim model by hand. Every converted rocket should still be checked against OpenRocket, real flight data, or other trusted simulation results before it is used for HIL testing or design decisions.

Major areas that still need improvement:

- More accurate CG and CP extraction/validation from OpenRocket.
- Better mass and inertia modeling for distributed components.
- More faithful aerodynamic coefficient generation instead of baseline approximations.
- Better rail/launch-contact modeling to avoid numerical instability at liftoff.
- More complete recovery modeling for drogue/main parachutes and deployment timing.
- Real pressure, temperature, and sensor-output modeling instead of placeholder columns.
- Better RACS/ACS control-surface modeling from actual geometry and actuator behavior.
- More robust motor matching when OpenRocket stores only partial motor identifiers.
- Automated validation against OpenRocket exported simulation CSVs.
- Cleaner handling of edge cases such as staged rockets, clustered motors, unusual fins, pods, boosters, and custom components.
- More complete generated HIL code that matches the final JSBSim/HIL communication architecture.

In short: this converter reduces setup work, but the generated model still needs engineering review and tuning.

## Quick Start

Run this from WSL:

```bash
cd open-rocket-to-jsbsim-converter
./convert_ork.sh "<path-to-your-openrocket-file.ork>" <rocket_name> --run
```

That command will:

1. Read the `.ork` file.
2. Generate JSBSim aircraft XML and C++ starter files.
3. Install the generated files into `~/jsbsim-rocket-hitl`.
4. Add/build the CMake targets.
5. Run the standalone JSBSim simulation.

If you only want to convert and build, but not run:

```bash
./convert_ork.sh "<path-to-your-openrocket-file.ork>" <rocket_name>
```

## Output Files

Generated files are written under:

```text
open rocket convert/<rocket_name>/
```

Inside that folder:

```text
aircraft/<rocket_name>/<rocket_name>.xml
aircraft/<rocket_name>/Engines/<motor>_engine.xml
aircraft/<rocket_name>/Engines/<motor>_nozzle.xml
aircraft/<rocket_name>/Systems/README.md
src/rocket_sim_<rocket_name>.cpp
src/hitl_sim_<rocket_name>.cpp
reports/conversion_report.txt
```

When `--install-jsbsim` is used, the same aircraft and C++ files are copied into `~/jsbsim-rocket-hitl` so JSBSim can build them.

## Manual Python Usage

You can also run the Python converter directly:

```bash
python3 ./openrocket_to_jsbsim.py \
  "<path-to-your-openrocket-file.ork>" \
  --name <rocket_name> \
  --install-jsbsim <path-to-jsbsim-rocket-hitl> \
  --build-jsbsim
```

To only generate files locally:

```bash
python3 ./openrocket_to_jsbsim.py \
  "<path-to-your-openrocket-file.ork>" \
  --name <rocket_name>
```

## How It Works

An `.ork` file is a ZIP file that contains OpenRocket XML. The converter opens that XML and walks through the rocket design tree.

It extracts:

- rocket name and comments
- body tube, nose cone, transition, and fin geometry
- freeform/RACS fin geometry when present
- mass overrides and component masses
- motor configuration and selected motor ID
- parachute diameter, drag coefficient, and deployment event
- launch rail length, launch angle, and launch direction
- wind speed, wind direction, and turbulence settings

Then it computes/estimates the JSBSim values needed for a first-pass model:

- reference area
- wing/fin area
- fin span/chord approximations
- mass and inertia placeholders
- CG estimate
- baseline drag/lift/stability coefficients
- recovery drag area
- rail contact behavior

Finally it writes JSBSim files using the VADL `summer_subscale_2026` layout as the reference structure.

## Motor Thrust Curves

The converter tries not to invent motor data.

OpenRocket `.ork` files often store the selected motor identity, but not the full thrust curve. For that reason, this project uses the OpenRocket motor database as the thrust source.

The converter searches for a motor database in this order:

1. A path passed with `--motor-db`.
2. The `OPENROCKET_MOTOR_DB` environment variable.
3. The local `motor_data/motors.db` file.
4. Common OpenRocket user motor database locations.
5. The OpenRocket published motor database, downloaded into `motor_data/` if needed.

The generated engine XML includes comments saying where the thrust data came from.

If a real motor curve cannot be found, the converter stops instead of making up thrust. For rough testing only, you can force placeholder thrust with:

```bash
--allow-placeholder-thrust
```

## Running The Generated JSBSim Model

After conversion/install/build, run the standalone sim from WSL:

```bash
cd ~/jsbsim-rocket-hitl
./build/rocket_sim_<rocket_name>
```

The HIL version is also generated and built:

```bash
./build/hitl_sim_<rocket_name>
```

Replace `<rocket_name>` with the name you used during conversion.

## CSV Output

Standalone simulations write trajectory CSVs into the JSBSim repo `data/` folder, for example:

```text
<path-to-jsbsim-rocket-hitl>/data/<rocket_name>_trajectory.csv
```

The CSV is formatted to be readable by the Helix Flight Analyzer where possible. It includes columns such as time, altitude, AGL altitude, velocity, acceleration, gyro placeholders, pressure/temperature placeholders, and event-style fields when available.

## Troubleshooting

### `python3: command not found`

Use Python from Windows or install Python in WSL:

```bash
sudo apt update
sudo apt install python3
```

### `ValueError: file is not an OpenRocket .ork zip file`

This usually means the path is wrong or WSL is receiving a Windows path in the wrong format.

Use WSL paths like:

```bash
/mnt/c/path/to/your/rocket.ork
```

not raw Windows paths like:

```text
C:\path\to\your\rocket.ork
```

### JSBSim builds but the rocket barely launches

Check the generated terminal output for:

- matched motor curve
- total impulse
- rocket mass
- rail exit time
- max altitude
- numerical divergence

Common causes:

- wrong motor matched
- mass too high or too low
- thrust table starts incorrectly
- launch rail/contact parameters are unstable
- aerodynamic coefficients are still rough baseline estimates

### Pressure or temperature plots are empty

OpenRocket does not provide live sensor pressure/temperature data. The converter can output placeholder columns so the analyzer can read the CSV, but those are not real simulated sensor models yet.

## Current Limitations

This is a first-pass converter, not a complete physics translation.

Known limitations:

- CG/CP are still partly estimated.
- Inertia values are rough placeholders.
- Aerodynamic coefficients are baseline approximations.
- RACS/ACS mechanics are generated as JSBSim properties and placeholder roll moments, not automatically inferred from OpenRocket behavior.
- Recovery is simplified to generated drag-area behavior.
- Pressure/temperature sensor outputs are placeholders unless a real atmosphere/sensor model is added.
- The generated HIL C++ file is starter scaffolding, not a full custom controller integration.

## Recommended Workflow

1. Build the rocket in OpenRocket.
2. Save the `.ork` file.
3. Run `convert_ork.sh` on the `.ork` file.
4. Build/run the generated JSBSim model.
5. Compare JSBSim output against OpenRocket altitude, velocity, acceleration, and apogee.
6. Tune JSBSim aero/mass/recovery values.
7. Only then use the generated HIL runner for hardware testing.

## Repository Contents

```text
openrocket_to_jsbsim.py   Main converter
convert_ork.sh            Easy WSL wrapper script
motor_data/               Local OpenRocket motor database cache
README.md                 This guide
.gitignore                Ignores generated outputs/cache
```

Generated outputs are intentionally ignored by Git so the repository stays focused on the converter logic.

