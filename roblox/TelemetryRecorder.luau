--!strict
-- Records gameplay telemetry in the same JSON Lines format that scpsl_cleanroom analyzes,
-- so the Roblox port can be measured and compared against the spec:
--   python -m scpsl_cleanroom compare port.jsonl --spec spec.json
--
-- Usage (server Script):
--   local Recorder = require(path.to.TelemetryRecorder)
--   Recorder.start()
--   Recorder.event(player, "door_open_start", "LCZ_A")  -- from your game code
--   ...
--   Recorder.dump()  -- prints lines prefixed with "TLM " (copy from Output), or
--   Recorder.post("http://localhost:8765/telemetry")  -- Studio, HTTP requests enabled
--
-- Players are tagged from attributes your game sets: "Role", "State", "Stamina".
-- Positions are written in meters (METERS_PER_STUD) to match the spec's units.

local HttpService = game:GetService("HttpService")
local Players = game:GetService("Players")
local RunService = game:GetService("RunService")

local Recorder = {}

Recorder.METERS_PER_STUD = 0.28
Recorder.SAMPLE_INTERVAL = 0.1
Recorder.MAX_LINES = 200000

local lines: { string } = {}
local connection: RBXScriptConnection? = nil
local t0 = 0

local function round(n: number, digits: number): number
	local m = 10 ^ digits
	return math.floor(n * m + 0.5) / m
end

local function snapshot(player: Player): { [string]: any }?
	local character = player.Character
	local root = character and character:FindFirstChild("HumanoidRootPart") :: BasePart?
	local humanoid = character and character:FindFirstChildOfClass("Humanoid")
	if not root or not humanoid then
		return nil
	end
	local p = root.Position * Recorder.METERS_PER_STUD
	local state = player:GetAttribute("State")
	if state == nil then
		state = if humanoid.MoveDirection.Magnitude > 0.01 then "move" else "idle"
	end
	local sample: { [string]: any } = {
		t = round(os.clock() - t0, 3),
		id = tostring(player.UserId),
		role = player:GetAttribute("Role") or "any",
		state = state,
		pos = { round(p.X, 3), round(p.Y, 3), round(-p.Z, 3) }, -- back to the spec's left-handed axes
		hp = round(humanoid.Health, 3),
	}
	local stamina = player:GetAttribute("Stamina")
	if typeof(stamina) == "number" then
		sample.stamina = round(stamina, 4)
	end
	return sample
end

local function push(sample: { [string]: any })
	if #lines < Recorder.MAX_LINES then
		table.insert(lines, HttpService:JSONEncode(sample))
	end
end

function Recorder.start()
	if connection then
		return
	end
	t0 = os.clock()
	local acc = 0
	connection = RunService.Heartbeat:Connect(function(dt)
		acc += dt
		if acc < Recorder.SAMPLE_INTERVAL then
			return
		end
		acc = 0
		for _, player in Players:GetPlayers() do
			local s = snapshot(player)
			if s then
				push(s)
			end
		end
	end)
end

function Recorder.stop()
	if connection then
		connection:Disconnect()
		connection = nil
	end
end

-- Logs an event with a fresh snapshot (hp after a hit, position at door use, ...).
function Recorder.event(player: Player, name: string, tag: string?)
	local s = snapshot(player) or { t = round(os.clock() - t0, 3), id = tostring(player.UserId) }
	s.event = name
	if tag then
		s.tag = tag
	end
	push(s)
end

function Recorder.dump()
	for _, line in lines do
		print("TLM " .. line)
	end
end

function Recorder.post(url: string)
	local chunk = 2000
	for i = 1, #lines, chunk do
		local body = table.concat(lines, "\n", i, math.min(i + chunk - 1, #lines))
		HttpService:PostAsync(url, body, Enum.HttpContentType.TextPlain)
	end
end

function Recorder.clear()
	table.clear(lines)
end

return Recorder
