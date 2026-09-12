# Design System

## Direction

**Scene:** a quiet broadcast control room before dawn, one operator watches two transmission paths under low ambient light. Controls are cool and precise; amber appears only when attention is required.

**Strategy:** restrained dark product UI. Near-black neutral architecture, cobalt for selected and primary actions, semantic colors for verified state. No decorative glass panels, gradients, or oversized metrics.

## Color Palette

Use OKLCH values directly.

```css
:root {
  --bg: oklch(0.105 0 0);
  --surface-1: oklch(0.145 0.008 260);
  --surface-2: oklch(0.185 0.012 260);
  --surface-3: oklch(0.235 0.014 260);
  --ink: oklch(0.955 0.008 260);
  --muted: oklch(0.720 0.018 260);
  --primary: oklch(0.620 0.170 260);
  --primary-hover: oklch(0.675 0.165 260);
  --accent: oklch(0.790 0.145 78);
  --success: oklch(0.735 0.145 155);
  --warning: oklch(0.790 0.145 78);
  --danger: oklch(0.650 0.190 28);
  --info: oklch(0.720 0.120 230);
  --focus: oklch(0.760 0.135 260);
  --border: oklch(0.315 0.018 260);
}
```

Status never relies on color alone. Pair every state color with a text label and compact icon or shape.

## Typography

- Family: `Inter`, `Segoe UI Variable`, `Segoe UI`, system-ui, sans-serif.
- Use one family throughout. Data such as bitrate and duration may use `ui-monospace`.
- Scale: 12, 14, 16, 20, 24, 30px. No fluid display sizes.
- Body line height 1.5; labels 1.35; headings 1.2.
- Headings use 600 weight. Body and controls use 400–550.

## Layout

- Desktop shell max width 1440px with 24px outer padding.
- Sticky 56px command bar contains product identity, overall state, Start monitoring, and Stop all.
- Source configuration spans the main width above outputs.
- Douyin and WeChat destination panels are equal columns at 960px and above; one column below.
- Monitoring and events sit below configuration. Event list owns its scroll region; the page must not create nested horizontal scroll.
- Mobile uses one column, 16px padding, and a bottom-safe action row when primary controls would leave the viewport.

## Components

- Corners: 10px controls, 12px panels, pill only for status chips.
- Prefer surface separation and 1px neutral borders; avoid wide decorative shadows.
- Inputs have persistent labels, helper/error text, visible focus ring, and reserved error space where practical.
- Buttons use verb + object labels. Primary actions are cobalt with near-white text. Destructive stop is neutral by default and danger-colored only in confirmation state.
- Skeletons preserve final layout. Empty states explain the next configuration action.
- Stream keys are password inputs; existing keys always render a fixed mask and are never placed back into the DOM value.

## Motion

- State transitions: 160–220ms ease-out.
- Animate opacity and transform only for toasts or expanding diagnostics.
- Live indicators may use a subtle opacity pulse; disable it under `prefers-reduced-motion`.
- No page-load choreography.

## Accessibility

- WCAG 2.2 AA minimum; body text target 7:1 where practical.
- Full keyboard path and visible focus.
- `aria-live` for operation results and state changes, not continuous metrics.
- Confirmation is required only for stopping active outputs.
- Loading disables the initiating control and communicates progress without blocking unrelated destination actions.
