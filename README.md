# 🎢 Bezier Curve Editor for Blender

**Bezier Curve Editor** is a powerful, floating UI overlay designed specifically for Blender animators. It brings advanced CSS/AfterEffects-style easing right into your Dopesheet and Graph Editor, allowing you to visually shape and apply interpolation curves to keyframe segments with zero friction.

---

## 🎨 The Interface (TUI Representation)

The addon injects a sleek, non-intrusive floating overlay right into your Timeline, Dopesheet, Info, or Graph Editor headers. 

```text
 ╭────────────────────────────────────────╮
 │    [ Bezier ]       [ Elastic ]        │
 │ ╭────────────────────────────────────╮ │
 │ │                                o   │ │
 │ │                               /    │ │
 │ │                              /     │ │
 │ │                             /      │ │
 │ │                            /       │ │
 │ │             o-------------o        │ │
 │ │            /                       │ │
 │ │           /                        │ │
 │ │          /                         │ │
 │ │         /                          │ │
 │ │   o----o                           │ │
 │ ╰────────────────────────────────────╯ │
 │  [  Center  ] [  Mirror  ] [  Link ● ] │
 │  [ Copy Sel ] [ Auto: ON ] [ Reset   ] │
 │ ┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈ │
 │  Built-in                              │
 │  [ Linear ] [ Ease ] [ In ] [ Out ]    │
 │  Custom: Bounce                        │
 │  [ Heavy  ] [ Light ]                  │
 │ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ │
 │             APPLY CURVE (3)            │
 ╰────────────────────────────────────────╯
```

---

## ✨ Core Features

### 1. Dual Engine Modes
- **Bezier Mode**: Classic 2D cubic Bezier handles (H1 and H2). Allows for ease-in, ease-out, and overshoot (values can go below 0 or above 1).
- **Elastic Mode**: Create bouncy, spring-driven overshoot animations. Features two interactive handles: **Amplitude** (bounce height) and **Period** (bounce frequency).

### 2. Interactive Viewport
- **Drag-and-Drop Handles**: Freely click and drag the interactive handles directly in the overlay.
- **Snapping**: Hold `Ctrl` to snap handles to the grid. Hold `Shift` to snap handles to the exact 0.0 or 1.0 edges.
- **Pan & Zoom**: The graph is fully navigable. Click and drag empty space to pan, or use the `Center` button to reset your view.

### 3. Smart Application & Batching
- **APPLY CURVE**: Select any number of keyframe segments in your Graph Editor or Dopesheet and hit **Apply Curve** (or press the **`Alt+E`** hotkey!). The curve will automatically bake into the selected segments.
- **Auto Apply**: When toggled **ON**, dragging the handles in the UI will *live-update* the selected keyframes in real-time.
- **Batch Processing**: The Apply button intelligently tracks how many curves you have selected, displaying the count dynamically (e.g., `APPLY CURVE (5)`).

### 4. Advanced "Link" Handle System
Working on a symmetrical camera sweep? Toggle the **Link ●** button. 
When enabled, moving the first handle will automatically mirror the exact opposite coordinates to the second handle, guaranteeing perfect symmetrical Ease-In / Ease-Out acceleration.

### 5. Graph Editor "Ghost" Overlay
Never guess what your curve will look like. When you modify handles in the Bezier Editor, a **semi-transparent ghost curve** is projected live onto your actual F-Curves in the Graph Editor, mapping the normalized shape exactly to the frame and value dimensions of your animation.

---

## 💾 Preset Management Engine

The addon features a robust, visual preset grid to store your favorite curves.

```text
  Built-in
 ╭────────╮ ╭────────╮ ╭────────╮
 │        │ │      / │ │      / │
 │   /    │ │     /  │ │     /  │
 │  /     │ │ o--o   │ │    /   │
 │ /      │ │        │ │   /    │
 ╰────────╯ ╰────────╯ ╰────────╯
   Linear      Ease      Ease In
```

- **Built-in Presets**: Factory defaults (`Linear`, `Ease`, `Ease In`, `Ease Out`, `Ease In-Out`) are securely locked at the top of your list.
- **Save Custom Presets**: Hit the **Save Preset** icon to permanently store your current curve shape.
- **Categorization**: Want to organize your presets? Rename a preset with a colon (e.g., `Bounce: Heavy`). The UI will automatically group it under a beautiful "Bounce" subheader!
- **Hover Animations**: Hover your mouse over any preset tile to see a live-animated dot sweep along the curve path, letting you feel the timing before you click.
- **Edit & Colorize**: Right-click any custom preset to bring up the edit menu. You can rename the preset and pick a custom background tile color.

---

## 🚀 How to Use It

1. **Open the UI**: Look at the header (top right) of your Dopesheet, Graph Editor, or Info panel. Click the `Bezier Curve Editor` toggle icon.
2. **Select Keyframes**: In the Dopesheet or Graph Editor, select the keyframes that make up the segment you want to ease.
3. **Shape the Curve**: Drag the handles in the UI to craft your easing shape.
4. **Apply**: Click `APPLY CURVE` (or use `Alt+E`).
5. **Copy Existing Curves**: Select an existing keyframe segment that has an easing shape you like, and click `Copy Sel.` to pull that curve into the UI editor to save as a preset.

---

## ⚙️ Configuration

Head to `Edit > Preferences > Add-ons > Bezier Curve Editor` to customize:
- **Redraw Load Threshold**: Performance tuning for slower machines.
- **Header Buttons**: Choose which editor headers (Timeline, Info, Graph Editor) display the toggle button.
- **Graph Height Ratio**: Adjust the default vertical height of the editor widget to match your screen size.

*(Note: The editor automatically inherits all colors directly from your active Blender Theme, seamlessly integrating into your workspace regardless of whether you use a Dark, Light, or Custom theme!)*
