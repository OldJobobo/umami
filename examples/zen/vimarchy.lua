-- Vimarchy's modal hint bindings, from its README at commit 3060197690
-- (https://github.com/clickety-clacks/vimarchy, MIT licensed).
-- Changed for the Zen flavor: wrapped so it does nothing for a user who
-- doesn't have Vimarchy installed, and the launcher is Super+' instead of
-- Alt+Space, which stock Omarchy gives to dictation.
do
local vimarchy = os.getenv("HOME") .. "/.config/omarchy/plugins/vimarchy/bin"
local probe = io.open(vimarchy .. "/open", "r")
if probe then
probe:close()
local hint_keys = "asdfghjklqwertyuiopzxcvbnm"

local function vimarchy_scale_bindings()
  hl.bind("CTRL + PLUS", hl.dsp.exec_cmd(vimarchy .. "/scale up"))
  hl.bind("CTRL + SHIFT + EQUAL", hl.dsp.exec_cmd(vimarchy .. "/scale up"))
  hl.bind("CTRL + MINUS", hl.dsp.exec_cmd(vimarchy .. "/scale down"))
end

local function vimarchy_escape_bindings()
  vimarchy_scale_bindings()
  hl.bind("SUPER + comma", hl.dsp.exec_cmd(vimarchy .. "/settings"))
  hl.bind("ESCAPE", hl.dsp.exec_cmd(vimarchy .. "/cancel"))
  hl.bind("ALT + SPACE", hl.dsp.exec_cmd(vimarchy .. "/cancel"))
  hl.bind("SUPER + ESCAPE", hl.dsp.submap("reset"))
end

hl.define_submap("vimarchy", function()
  for slot = 1, #hint_keys do
    local letter = hint_keys:sub(slot, slot)
    hl.bind(letter, hl.dsp.exec_cmd(vimarchy .. "/press " .. letter))
    hl.bind("ALT + " .. letter, hl.dsp.exec_cmd(vimarchy .. "/press " .. letter .. " alt"))
    hl.bind("ALT + " .. letter, hl.dsp.exec_cmd(vimarchy .. "/release " .. letter), { release = true })
    hl.bind(letter, hl.dsp.exec_cmd(vimarchy .. "/release " .. letter), { release = true })
  end
  vimarchy_escape_bindings()
end)

hl.define_submap("vimarchy-double", function()
  for first = 1, #hint_keys do
    local letter = hint_keys:sub(first, first)
    hl.bind(letter, hl.dsp.submap("vimarchy-" .. letter))
    hl.bind("ALT + " .. letter, hl.dsp.submap("vimarchy-" .. letter))
  end
  vimarchy_escape_bindings()
end)

for first = 1, #hint_keys do
  local first_letter = hint_keys:sub(first, first)
  hl.define_submap("vimarchy-" .. first_letter, function()
    for second = 1, #hint_keys do
      local second_letter = hint_keys:sub(second, second)
      local hint = first_letter .. second_letter
      hl.bind(second_letter, hl.dsp.exec_cmd(vimarchy .. "/press " .. hint))
      hl.bind("ALT + " .. second_letter, hl.dsp.exec_cmd(vimarchy .. "/press " .. hint .. " alt"))
      hl.bind("ALT + " .. second_letter, hl.dsp.exec_cmd(vimarchy .. "/release " .. hint), { release = true })
      hl.bind(second_letter, hl.dsp.exec_cmd(vimarchy .. "/release " .. hint), { release = true })
    end
    vimarchy_escape_bindings()
  end)
end

hl.define_submap("vimarchy-workspaces", function()
  for key in ("1234567890s"):gmatch(".") do
    hl.bind(key, hl.dsp.exec_cmd(vimarchy .. "/workspace-select " .. key))
    hl.bind("ALT + " .. key, hl.dsp.exec_cmd(vimarchy .. "/workspace-select " .. key .. " alt"))
  end
  hl.bind("ESCAPE", hl.dsp.exec_cmd(vimarchy .. "/cancel"))
  hl.bind("ALT + ESCAPE", hl.dsp.exec_cmd(vimarchy .. "/cancel"))
  hl.bind("ALT + SPACE", hl.dsp.exec_cmd(vimarchy .. "/cancel"))
  hl.bind("SUPER + ESCAPE", hl.dsp.submap("reset"))
  vimarchy_scale_bindings()
end)

hl.define_submap("vimarchy-swap", function()
  for slot = 1, #hint_keys do
    local letter = hint_keys:sub(slot, slot)
    hl.bind(letter, hl.dsp.exec_cmd(vimarchy .. "/swap-press " .. letter))
    hl.bind(letter, hl.dsp.exec_cmd(vimarchy .. "/swap-release " .. letter), { release = true })
  end
  vimarchy_escape_bindings()
end)

hl.define_submap("vimarchy-swap-double", function()
  for first = 1, #hint_keys do
    local letter = hint_keys:sub(first, first)
    hl.bind(letter, hl.dsp.submap("vimarchy-swap-" .. letter))
  end
  vimarchy_escape_bindings()
end)

for first = 1, #hint_keys do
  local first_letter = hint_keys:sub(first, first)
  hl.define_submap("vimarchy-swap-" .. first_letter, function()
    for second = 1, #hint_keys do
      local second_letter = hint_keys:sub(second, second)
      local hint = first_letter .. second_letter
      hl.bind(second_letter, hl.dsp.exec_cmd(vimarchy .. "/swap-press " .. hint))
      hl.bind(second_letter, hl.dsp.exec_cmd(vimarchy .. "/swap-release " .. hint), { release = true })
    end
    vimarchy_escape_bindings()
  end)
end

-- After a normal selection, only a quick repeat of that hint's final letter is
-- intercepted. Every other key continues to the newly focused application.
for slot = 1, #hint_keys do
  local letter = hint_keys:sub(slot, slot)
  hl.define_submap("vimarchy-double-tap-" .. letter, function()
    hl.bind(letter, hl.dsp.exec_cmd(vimarchy .. "/double-tap " .. letter))
    hl.bind("ALT + " .. letter, hl.dsp.exec_cmd(vimarchy .. "/double-tap " .. letter .. " alt"))
    hl.bind("ESCAPE", hl.dsp.exec_cmd(vimarchy .. "/cancel"))
    hl.bind("ALT + SPACE", hl.dsp.exec_cmd(vimarchy .. "/cancel"))
    hl.bind("SUPER + ESCAPE", hl.dsp.submap("reset"))
  end)
end

hl.define_submap("vimarchy-settings", function()
  hl.bind("ESCAPE", hl.dsp.exec_cmd(vimarchy .. "/cancel"))
  hl.bind("ALT + SPACE", hl.dsp.exec_cmd(vimarchy .. "/cancel"))
  hl.bind("SUPER + ESCAPE", hl.dsp.submap("reset"))
end)

o.bind("SUPER + APOSTROPHE", "Vimarchy window hints", vimarchy .. "/open")
end
end
