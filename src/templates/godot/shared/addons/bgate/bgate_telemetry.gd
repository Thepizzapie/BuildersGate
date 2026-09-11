extends Node
## Builders Gate telemetry — the bridge between "it feels wrong" and a number.
##
## Autoloaded as BGateTelemetry. Two delivery paths, chosen by build target:
##
##  * NATIVE — appends one JSON object per line to the path in the
##    BGATE_TELEMETRY env var. When that var is unset (you just opened the game
##    normally), this does nothing at all — zero cost, no file, no error. The
##    recorder ingests the file on stop.
##
##  * WEB (WASM in the dashboard's /play iframe) — a browser has no env var and
##    no file access, so the file path is dead there. Instead we self-discover
##    the active recording by polling the SAME-ORIGIN app at
##    /api/playtest/status, and POST batched events to
##    /api/playtest/<id>/events. No dashboard wiring, no query param: the game
##    asks the server whether a recording is live and streams into it. This is
##    why an in-app playtest used to capture only your voice — the web build had
##    no path to the event store at all.
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
const WEB_POLL_INTERVAL := 2.0
const WEB_BUFFER_MAX := 400  # cap so a long silent stretch can't grow unbounded
const SCHEMA_VERSION := 1

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

# --- web delivery state ----------------------------------------------------
var _web := false
var _origin := ""
var _web_session := -1
var _web_buffer: Array = []
var _poll_req: HTTPRequest = null
var _flush_req: HTTPRequest = null
var _poll_inflight := false
var _flush_inflight := false
var _poll_accum := 0.0


func _ready() -> void:
	_t0_msec = Time.get_ticks_msec()

	var autoquit := OS.get_environment("BGATE_AUTOQUIT")
	if autoquit != "":
		_autoquit_after = float(autoquit)

	if OS.has_feature("web"):
		_web_init()
		return

	var path := OS.get_environment("BGATE_TELEMETRY")
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


## --- web mode --------------------------------------------------------------

func _web_init() -> void:
	_web = true
	# The iframe is served from the app's own origin (/play/...), so relative
	# API paths resolve to the recorder. Resolve it explicitly for HTTPRequest,
	# which wants an absolute URL even in web export.
	var origin: Variant = JavaScriptBridge.eval("window.location.origin", true)
	_origin = str(origin) if origin != null else ""
	if _origin == "":
		# No origin (opened as a bare file?) — nothing to POST to. Stay inert
		# except for autoquit.
		set_process(_autoquit_after > 0.0)
		return
	_poll_req = HTTPRequest.new()
	_poll_req.name = "BGatePoll"
	add_child(_poll_req)
	_poll_req.request_completed.connect(_on_poll_done)
	_flush_req = HTTPRequest.new()
	_flush_req.name = "BGateFlush"
	add_child(_flush_req)
	_flush_req.request_completed.connect(_on_flush_done)
	set_process(true)
	_poll_status()  # discover any recording that's already live before we loaded


func _poll_status() -> void:
	if _poll_inflight or _poll_req == null:
		return
	_poll_inflight = true
	var err := _poll_req.request(_origin + "/api/playtest/status")
	if err != OK:
		_poll_inflight = false


func _on_poll_done(_result: int, code: int, _headers: PackedStringArray, body: PackedByteArray) -> void:
	_poll_inflight = false
	if code != 200:
		return
	var parsed: Variant = JSON.parse_string(body.get_string_from_utf8())
	if typeof(parsed) != TYPE_DICTIONARY:
		return
	var rec: Variant = parsed.get("recording")
	# A live WEB recording is a dict with a non-native flag. Native sessions own
	# their own file path; we must not double-report into them.
	var active := typeof(rec) == TYPE_DICTIONARY and not bool(rec.get("native", false))
	if active:
		var sid := int(rec.get("id", -1))
		if sid != _web_session:
			_web_session = sid
			_enabled = true
			emit_event("session_open", {
				"godot": Engine.get_version_info().string,
				"scene": get_tree().current_scene.name if get_tree().current_scene else "",
				"web": true,
			})
	elif _enabled:
		# Recording ended. Flush whatever tail we still hold, then go inert.
		emit_event("session_close", {"elapsed_s": _elapsed})
		_flush_web(true)
		_enabled = false
		_web_session = -1


func _flush_web(final: bool = false) -> void:
	if _flush_req == null or _web_session < 0:
		_web_buffer.clear()
		return
	if _flush_inflight or _web_buffer.is_empty():
		return
	var batch: Array = _web_buffer
	_web_buffer = []
	_flush_inflight = true
	var payload := JSON.stringify({"events": batch})
	var err := _flush_req.request(
		_origin + "/api/playtest/%d/events" % _web_session,
		PackedStringArray(["Content-Type: application/json"]),
		HTTPClient.METHOD_POST, payload)
	if err != OK:
		# Could not even start the request. On a normal flush, re-queue so the
		# next tick retries; on the final flush there is no next tick, so drop.
		_flush_inflight = false
		if not final:
			_web_buffer = batch + _web_buffer


func _on_flush_done(_result: int, _code: int, _headers: PackedStringArray, _body: PackedByteArray) -> void:
	_flush_inflight = false


func _process(delta: float) -> void:
	_elapsed += delta

	if _web:
		_poll_accum += delta
		if _poll_accum >= WEB_POLL_INTERVAL:
			_poll_accum = 0.0
			_poll_status()
		if _enabled:
			_since_flush += delta
			_fps_accum += delta
			if _fps_accum >= _fps_interval:
				_fps_accum = 0.0
				emit_event("fps", {"fps": Engine.get_frames_per_second()})
			if _since_flush >= FLUSH_INTERVAL:
				_since_flush = 0.0
				_flush_web()
	elif _enabled:
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
		if _web:
			_flush_web(true)
		else:
			_flush()
		get_tree().quit()


## Record one event. `kind` is a short name; `data` is any JSON-able dict.
func emit_event(kind: String, data: Dictionary = {}) -> void:
	if not _enabled:
		return
	var event := {
		"schema": SCHEMA_VERSION,
		"ts": Time.get_unix_time_from_system(),
		"t": float(Time.get_ticks_msec() - _t0_msec) / 1000.0,
		"kind": kind,
		"data": data,
	}
	if _web:
		_web_buffer.append(event)
		if _web_buffer.size() > WEB_BUFFER_MAX:
			_web_buffer = _web_buffer.slice(_web_buffer.size() - WEB_BUFFER_MAX)
		return
	_file.store_line(JSON.stringify(event))


func _flush() -> void:
	if _file != null:
		# Godot buffers; a crash mid-session would lose exactly the events that
		# explain the crash. Flush on a timer so the tail always survives.
		_file.flush()


func _notification(what: int) -> void:
	if what == NOTIFICATION_WM_CLOSE_REQUEST or what == NOTIFICATION_PREDELETE:
		if _web:
			if _enabled:
				emit_event("session_close", {"elapsed_s": _elapsed})
				_flush_web(true)
				_enabled = false
			return
		if _enabled and _file != null:
			emit_event("session_close", {"elapsed_s": _elapsed})
			_file.flush()
			_file.close()
			_file = null
			_enabled = false
