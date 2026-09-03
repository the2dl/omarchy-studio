-- Nested Hyprland for verifying the screenshare-exclude plugin.
-- Lua so hyprctl eval works, which is how window rules get installed at runtime.
hl.monitor({ output = "", mode = "preferred", position = "auto", scale = 1 })

hl.config({
    misc = {
        background_color        = "rgb(00c000)",  -- the KNOWN colour behind the window
        disable_hyprland_logo   = true,
        disable_splash_rendering = true,
        force_default_wallpaper = 0,
        disable_autoreload      = true,
    },
    animations  = { enabled = false },
    decoration  = { rounding = 0, blur = { enabled = false } },
    general     = { border_size = 0, gaps_in = 0, gaps_out = 0 },
    -- No `render.screenshare_exclude_windows` here: that key only exists in the patched
    -- binary, and a STOCK Hyprland rejects it, trips Lua emergency mode and paints an
    -- error banner into the frame. Run the extracted stock binary instead (README).
})

-- In the CONFIG, not installed dynamically: `hyprctl plugin load` reloads the config,
-- which drops rules added with `hyprctl eval hl.window_rule(...)`. That silently
-- un-marked the test window on every plugin load and made the whole rig measure nothing.
hl.window_rule({ match = { class = "^foot$" }, float = true, border_size = 0, rounding = 0, no_shadow = true })
hl.window_rule({ match = { title = "^excl$" }, no_screen_share = true })
