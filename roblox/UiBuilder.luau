--!strict
-- Builds ScreenGuis from the generated UiLayout module
-- (python -m scpsl_cleanroom ui <exported prefabs> -o out -> out/UiLayout.luau).
--
--   local build = require(path.to.UiBuilder)
--   build(require(path.to.UiLayout), player.PlayerGui, { ["Bauhaus"] = Font.new("rbxassetid://...") })
--
-- `fonts` maps the Unity font asset name to a Roblox Font you uploaded; missing fonts fall back
-- to the default. Text sizes are scaled from the Unity canvas height to the current screen.

type Node = { [string]: any }

local SKIP = {
	ClassName = true, Children = true, unity_path = true, source_component = true, Font = true,
	TextSizeScale = true, TextSizeMin = true, TextSizeMax = true, FontStyle = true,
	CharacterSpacing = true, LineSpacing = true, Position = true, Size = true, AnchorPoint = true,
	TextColor3 = true, ImageColor3 = true, Image = true,
}

local function udim2(v: { number }): UDim2
	return UDim2.new(v[1], v[2], v[3], v[4])
end

local function create(node: Node, parent: Instance, fonts: { [string]: Font }, refHeight: number): Instance
	local inst = Instance.new(node.ClassName) :: any
	inst.Name = node.Name
	if node.ClassName ~= "ScreenGui" then
		inst.AnchorPoint = Vector2.new(node.AnchorPoint[1], node.AnchorPoint[2])
		inst.Position = udim2(node.Position)
		inst.Size = udim2(node.Size)
		inst.BackgroundTransparency = 1
	end
	if node.TextColor3 then
		inst.TextColor3 = Color3.new(node.TextColor3[1], node.TextColor3[2], node.TextColor3[3])
		inst.TextXAlignment = Enum.TextXAlignment[node.TextXAlignment]
		inst.TextYAlignment = Enum.TextYAlignment[node.TextYAlignment]
		local fontName = node.Font and string.gsub(string.match(node.Font, "([^/\\]+)$") or "", "%.[^%.]+$", "")
		if fontName and fonts[fontName] then
			inst.FontFace = fonts[fontName]
		end
		if node.TextSizeScale and not node.TextScaled then
			-- keep the size proportional to screen height like a Unity CanvasScaler
			local camera = workspace.CurrentCamera
			local h = camera and camera.ViewportSize.Y or refHeight
			inst.TextSize = math.max(1, math.floor(node.TextSizeScale * h + 0.5))
		end
	end
	if node.ImageColor3 then
		inst.ImageColor3 = Color3.new(node.ImageColor3[1], node.ImageColor3[2], node.ImageColor3[3])
		inst:SetAttribute("UnitySprite", node.Image)
	end
	for key, value in node do
		if not SKIP[key] and key ~= "Name" then
			pcall(function()
				inst[key] = value
			end)
		end
	end
	inst:SetAttribute("UnityPath", node.unity_path)
	for _, child in node.Children do
		create(child, inst, fonts, refHeight)
	end
	inst.Parent = parent
	return inst
end

return function(layout: { Node }, parent: Instance, fonts: { [string]: Font }?): { Instance }
	local built = {}
	for _, tree in layout do
		table.insert(built, create(tree.root, parent, fonts or {}, tree.reference_resolution[2]))
	end
	return built
end
