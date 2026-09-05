@echo off
REM EMERGENCY STOP for the project in this directory (or the one named):
REM kills every agent, reaps orphans, turns auto-deploy off. Works when the
REM dashboard is gone or wedged, which is the whole point of having it here.
python -m bgate_cli.main panic %*
