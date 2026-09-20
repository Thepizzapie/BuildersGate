extends Node
class_name BGInventory
## A slot-based inventory: string item ids, stacks, a capacity, and signals
## for everything that changes. No item database is assumed: an id is
## whatever your game calls the thing, and `stack_sizes` overrides the
## default stack per id ("arrow": 99, "sword": 1).
##
## Add as a child of the player (or an autoload for a party inventory).
## Every mutating call returns what it could NOT do, so the caller decides
## whether an overflow drops on the floor or refuses the pickup.

## Some slot changed. Cheap to connect a whole UI to.
signal changed
## `count` of `item_id` went in (after stacking); `overflow` did not fit.
signal item_added(item_id: String, count: int, overflow: int)
## `count` of `item_id` came out; `missing` was asked for but not held.
signal item_removed(item_id: String, count: int, missing: int)
## An add could not place everything.
signal full(item_id: String, overflow: int)

## Number of slots. 0 means unlimited.
@export var capacity := 20
## Stack size for any id not in `stack_sizes`.
@export var default_stack := 1
## Per-id stack sizes: {"arrow": 99, "potion": 5}.
@export var stack_sizes: Dictionary = {}

## Slots in order. Each is null or {"id": String, "count": int}.
var slots: Array = []


func _ready() -> void:
	if slots.is_empty() and capacity > 0:
		slots.resize(capacity)


## How many of `item_id` fit in one slot.
func stack_size(item_id: String) -> int:
	return int(stack_sizes.get(item_id, default_stack))


## Put `count` of `item_id` in, filling existing stacks first. Returns the
## overflow that did not fit (0 when everything went in).
func add(item_id: String, count: int = 1) -> int:
	if count <= 0 or item_id.is_empty():
		return 0
	var left := count
	var per := stack_size(item_id)
	# Top up stacks that already hold this id.
	for slot in slots:
		if left == 0:
			break
		if slot != null and slot["id"] == item_id and slot["count"] < per:
			var room: int = per - slot["count"]
			var take: int = mini(room, left)
			slot["count"] += take
			left -= take
	# Then open slots.
	var i := 0
	while left > 0:
		if i >= slots.size():
			if capacity > 0:
				break
			slots.append(null)
		if slots[i] == null:
			var take: int = mini(per, left)
			slots[i] = {"id": item_id, "count": take}
			left -= take
		i += 1
	var placed := count - left
	if placed > 0:
		item_added.emit(item_id, placed, left)
		changed.emit()
		BGateTelemetry.emit_event("inventory_add", {
			"item": item_id, "count": placed, "overflow": left,
		})
	if left > 0:
		full.emit(item_id, left)
	return left


## Take `count` of `item_id` out, emptying the smallest stacks first.
## Returns how many were asked for but not held (0 when all came out).
func remove(item_id: String, count: int = 1) -> int:
	if count <= 0 or item_id.is_empty():
		return 0
	var left := count
	var holding: Array = []
	for i in slots.size():
		if slots[i] != null and slots[i]["id"] == item_id:
			holding.append(i)
	holding.sort_custom(func(a, b): return slots[a]["count"] < slots[b]["count"])
	for i in holding:
		if left == 0:
			break
		var take: int = mini(slots[i]["count"], left)
		slots[i]["count"] -= take
		left -= take
		if slots[i]["count"] == 0:
			slots[i] = null
	var taken := count - left
	if taken > 0:
		item_removed.emit(item_id, taken, left)
		changed.emit()
		BGateTelemetry.emit_event("inventory_remove", {
			"item": item_id, "count": taken, "missing": left,
		})
	return left


## Total held of `item_id` across every stack.
func count_of(item_id: String) -> int:
	var total := 0
	for slot in slots:
		if slot != null and slot["id"] == item_id:
			total += slot["count"]
	return total


func has(item_id: String, count: int = 1) -> bool:
	return count_of(item_id) >= count


## Could `count` of `item_id` be added without overflow? Pure; nothing moves.
func can_add(item_id: String, count: int = 1) -> bool:
	if capacity == 0:
		return true
	var room := 0
	var per := stack_size(item_id)
	for slot in slots:
		if slot == null:
			room += per
		elif slot["id"] == item_id:
			room += per - slot["count"]
		if room >= count:
			return true
	return room >= count


func is_empty() -> bool:
	for slot in slots:
		if slot != null:
			return false
	return true


## Occupied slot count.
func used_slots() -> int:
	var n := 0
	for slot in slots:
		if slot != null:
			n += 1
	return n


## Every distinct id held, with totals: {"arrow": 30, "sword": 1}.
func totals() -> Dictionary:
	var out := {}
	for slot in slots:
		if slot != null:
			out[slot["id"]] = int(out.get(slot["id"], 0)) + slot["count"]
	return out


func clear() -> void:
	for i in slots.size():
		slots[i] = null
	changed.emit()


## Swap two slots (a drag-and-drop UI's one operation).
func swap(a: int, b: int) -> void:
	if a < 0 or b < 0 or a >= slots.size() or b >= slots.size() or a == b:
		return
	var tmp = slots[a]
	slots[a] = slots[b]
	slots[b] = tmp
	changed.emit()


## Plain data for a save file. `from_dict` reads it back.
func to_dict() -> Dictionary:
	var out: Array = []
	for slot in slots:
		out.append(null if slot == null else slot.duplicate())
	return {"capacity": capacity, "slots": out}


func from_dict(data: Dictionary) -> void:
	capacity = int(data.get("capacity", capacity))
	slots.clear()
	for slot in data.get("slots", []):
		slots.append(null if slot == null else {
			"id": String(slot["id"]), "count": int(slot["count"])})
	if capacity > 0 and slots.size() < capacity:
		slots.resize(capacity)
	changed.emit()
