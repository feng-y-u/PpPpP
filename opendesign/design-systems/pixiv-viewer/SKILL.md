# Pixiv Viewer Design System

A dark-themed design system for a Pixiv browsing application. Purple accent, deep dark background, glass-morphism effects.

## Visual Language

- **Mood**: Dark, sophisticated, content-forward
- **Temperature**: Cool-dark, near-black backgrounds with warm purple accents
- **Density**: Comfortable spacing, generous whitespace
- **Corners**: Rounded (12px cards, 8px buttons, 6px nav links)

## Core Tokens

See `tokens/colors_and_type.css` for all design tokens.

## Component Inventory

- **Navbar**: Sticky, glass (`rgba(11,11,18,.82)` + `blur(20px)`), 0.8rem nav links, active state with purple bg
- **Search Bar**: Dark input group with purple focus glow, integrated dropdown + text input + button
- **Cards**: `rgba(255,255,255,.03)` bg, subtle border, 12px radius, hover lift (`translateY(-3px)`)
- **Buttons**: 8px radius, 500 weight, subtle hover transitions
- **Badges**: Dark semi-transparent bg with backdrop blur, 5px radius
- **Tags**: Small (0.65rem), muted color, purple hover state
- **Progress Bars**: 4-6px height, accent color fill
- **Lightbox**: Full-viewport modal, centered image, floating controls at 50% opacity
- **Modals**: Dark bg (`#1a1a2e`), subtle border, clean header/footer dividers
- **Toast**: Glass dark bg (`rgba(30,27,46,.92)`), blur, red tint for errors / green for success

## Typography Scale

| Token | Size | Weight | Usage |
|-------|------|--------|-------|
| `--fs-hero` | 1.8rem | 700 | Page hero title |
| `--fs-brand` | 1.15rem | 700 | Navbar brand |
| `--fs-title` | 0.85rem | 600 | Card titles |
| `--fs-body` | 0.8rem | 400 | Body/nav text |
| `--fs-small` | 0.72rem | 500 | Meta/secondary |
| `--fs-tiny` | 0.65rem | 500 | Tags/badges |

## Related

- Used by: Pixiv Viewer Flask application
- Template: Dark art/gallery browsing context
