extends RefCounted
class_name ResDir
## ResDir.files(path, ext) — list files under a res:// directory by extension,
## correctly whether the project is running from the editor OR an exported
## pck.
##
## THE MEASURED FAILURE this exists to prevent: eight scripts in a shipped
## project listed a data folder with DirAccess.get_files() and filtered the
## result with ends_with(".tres"). In the editor, where files are named
## normally on disk, that filter matches. In an EXPORTED pck, Godot's remap
## system lists every file DirAccess sees as the literal on-disk name, which
## for most resource types is "foo.tres.remap" (uncompressed export) or
## otherwise carries an extra suffix — so ends_with(".tres") matches NOTHING
## and the "directory" reads as empty. The exported build then boots into
## whatever the empty-data fallback path does — in EXIT 67's case, nothing
## at all.
##
## Use this INSTEAD of a raw get_files()/list_dir_begin + ends_with pair
## anywhere a script enumerates its own data files at runtime.
static func files(path: String, ext: String) -> PackedStringArray:
	var out := PackedStringArray()
	var suffix := ext if ext.begins_with(".") else "." + ext
	for name in DirAccess.get_files_at(path):
		var stripped := _strip_export_suffix(name)
		if stripped.ends_with(suffix):
			out.append(stripped)
	return out


## The bare filename an export-time remap or import sidecar name resolves to,
## e.g. "foo.tres.remap" -> "foo.tres", "foo.png.import" -> unchanged (the
## .import sidecar is never the asset itself and callers list the SOURCE
## name, not the sidecar).
static func _strip_export_suffix(name: String) -> String:
	if name.ends_with(".remap"):
		return name.substr(0, name.length() - ".remap".length())
	if name.ends_with(".import"):
		return name.substr(0, name.length() - ".import".length())
	return name


## Same shape as files(), but returns the full res:// path for each entry —
## what most callers actually want to hand to load()/FileAccess.open().
static func paths(path: String, ext: String) -> PackedStringArray:
	var out := PackedStringArray()
	var base := path if path.ends_with("/") else path + "/"
	for name in files(path, ext):
		out.append(base + name)
	return out
