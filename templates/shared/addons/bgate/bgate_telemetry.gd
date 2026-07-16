extends Node
## Builders Gate telemetry — the bridge between "it feels wrong" and a number.
##
## Autoloaded as BGateTelemetry. Appends one JSON object per line to the path in
## the BGATE_TELEMETRY env var. When that var is unset (you just opened the game
## normally), this does nothing at all — zero cost, no file, no error.
##
## Every event carries `ts`, a UNIX WALL-CLOCK timestamp, not seconds-since-boot.
## That is deliberate: the recorder's clock starts when recording starts, which is
## not when the game starts. Wall clock is the only shared axis between a process
## that launched whenever and a recorder that started whenever else. `t` is also
## included for human reading, but the aligner uses `ts`.
##
## Usage from anywhere:
##     BGateTelemetry.emit_event("jump", {"air_time": 0.92, "peak_h": 2.4})

const FLUSH_INTERVAL := 1.0

var _file: FileAccess = null
var _t0_msec: int = 0
var _since_flush := 0.0
var _enabled := false
var _fps_accum := 0.0
var _fps_interval := 2.0

## Auto-quit after N seconds. Set BGATE_AUTOQUIT=<seconds> to run the game
## unattended (headless smoke tests, CI) without a human to close the window.
var _autoquit_after := 0.0
var _elapsed := 0.0


func _ready() -> void:
	var path := OS.get_environment("BGATE_TELEMETRY")
	_t0_msec = Time.get_ticks_msec()

	var autoquit := OS.get_environment("BGATE_AUTOQUIT")
	if autoquit != "":
		_autoquit_after = float(autoquit)

	if path == "":
		# Not under a playtest session. Stay completely inert.
		set_process(_autoquit_after > 0.0)
		return

	# WRITE_READ keeps an existing file; open() truncates. A session appends.
	_file = FileAccess.open(path, FileAccess.WRITE_READ)
	if _file == null:
		push_warning("BGateTelemetry: cannot open %s (%d)" % [path, FileAccess.get_open_error()])
		set_process(_autoquit_after > 0.0)
		return
	_file.seek_end()
	_enabled = true
	set_process(true)
	emit_event("session_open", {
		"godot": Engine.get_version_info().string,
		"scene": get_tree().current_scene.name if get_tree().current_scene else "",
	})


func _process(delta: float) -> void:
	_elapsed += delta

	if _enabled:
		_since_flush += delta
		_fps_accum += delta
		if _fps_accum >= _fps_interval:
			_fps_accum = 0.0
			# fps is sampled, not derived from this frame — a single frame's delta
			# is noise, and "the framerate tanked here" needs the trend.
			emit_event("fps", {"fps": Engine.get_frames_per_second()})
		if _since_flush >= FLUSH_INTERVAL:
			_since_flush = 0.0
			_flush()

	if _autoquit_after > 0.0 and _elapsed >= _autoquit_after:
		emit_event("autoquit", {"after_s": _elapsed})
		_flush()
		get_tree().quit()


## Record one event. `kind` is a short name; `data` is any JSON-able dict.
func emit_event(kind: String, data: Dictionary = {}) -> void:
	if not _enabled:
		return
	var line := JSON.stringify({
		"ts": Time.get_unix_time_from_system(),
		"t": float(Time.get_ticks_msec() - _t0_msec) / 1000.0,
		"kind": kind,
		"data": data,
	})
	_file.store_line(line)


func _flush() -> void:
	if _file != null:
		# Godot buffers; a crash mid-session would lose exactly the events that
		# explain the crash. Flush on a timer so the tail always survives.
		_file.flush()


func _notification(what: int) -> void:
	if what == NOTIFICATION_WM_CLOSE_REQUEST or what == NOTIFICATION_PREDELETE:
		if _enabled and _file != null:
			emit_event("session_close", {"elapsed_s": _elapsed})
			_file.flush()
			_file.close()
			_file = null
			_enabled = false
