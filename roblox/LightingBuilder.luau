--!strict
-- Builds lights and Lighting settings from the generated LightingData module
-- (python -m scpsl_cleanroom assets ... -> out/LightingData.luau).
--
--   local build = require(path.to.LightingBuilder)
--   build(require(path.to.LightingData), workspace)

local Lighting = game:GetService("Lighting")

local function color3(c: { number }): Color3
	return Color3.new(c[1], c[2], c[3])
end

local function cframe(c: { number }): CFrame
	return CFrame.new(c[1], c[2], c[3], c[4], c[5], c[6], c[7], c[8], c[9], c[10], c[11], c[12])
end

local function applyLighting(settings: { [string]: any })
	for key, value in settings do
		if key == "source" or key == "fog_note" then
			continue
		end
		if typeof(value) == "table" then
			(Lighting :: any)[key] = color3(value)
		else
			(Lighting :: any)[key] = value
		end
	end
end

return function(data: { [string]: any }, parent: Instance): Folder
	local folder = Instance.new("Folder")
	folder.Name = "PortedLights"

	for _, rec in data.Lights do
		if rec.class == "Directional" then
			local d = rec.sun_direction
			-- Roblox's sun follows ClockTime/GeographicLatitude; keep the direction for manual setup.
			Lighting:SetAttribute("PortedSunDirection", Vector3.new(d[1], d[2], d[3]))
			continue
		end
		local anchor = Instance.new("Part")
		anchor.Name = rec.name
		anchor.Size = Vector3.new(0.2, 0.2, 0.2)
		anchor.Transparency = 1
		anchor.Anchored = true
		anchor.CanCollide = false
		anchor.CanQuery = false
		anchor.CanTouch = false
		anchor.CastShadow = false
		anchor.CFrame = cframe(rec.cframe)
		anchor:SetAttribute("Source", rec.source)

		local light = Instance.new(rec.class) :: any
		light.Color = color3(rec.color)
		light.Brightness = rec.brightness
		light.Range = rec.range
		light.Shadows = rec.shadows
		light.Enabled = rec.enabled
		if rec.angle then
			light.Angle = rec.angle
			light.Face = Enum.NormalId.Front
		end
		light.Parent = anchor
		anchor.Parent = folder
	end

	if data.Lighting then
		applyLighting(data.Lighting)
	end
	folder.Parent = parent
	return folder
end
