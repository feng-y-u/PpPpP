# Pixiv Viewer — Light Design System

A light, Japanese-minimalist design system for a Pixiv browsing application. Warm off-white background, coral accent, clean typography.

## Visual Language

- **Mood**: Clean, airy, content-forward, restrained
- **Temperature**: Warm-neutral (off-white #f5f5f0 base, coral #d4436b accent)
- **Density**: Generous whitespace, comfortable reading rhythm
- **Corners**: Subtle (8px max, mostly 4px)
- **Philosophy**: The image is the hero — UI recedes into the background

## Core Tokens

See `tokens/colors_and_type.css` for all design tokens.

## Component Principles

- **Navbar**: Glass white (`rgba(245,245,240,.88)` + `blur(16px)`), thin bottom border, minimal
- **Cards**: Minimal — white bg, no border, subtle shadow on hover only
- **Buttons**: Compact, 6px radius, coral accent, text labels only (no icons)
- **Typography**: System + Hiragino Sans, generous line-height
- **Images**: Full-bleed in cards, subtle placeholder bg

## Typography Scale

| Token | Size | Weight | Usage |
|-------|------|--------|-------|
| `--fs-hero` | 1.6rem | 600 | Page hero title |
| `--fs-brand` | 1rem | 600 | Navbar brand |
| `--fs-title` | 0.85rem | 500 | Card titles |
| `--fs-body` | 0.82rem | 400 | Body/nav text |
| `--fs-small` | 0.75rem | 400 | Meta/secondary |
| `--fs-tiny` | 0.68rem | 400 | Tags/badges |

## Related

- Used by: Pixiv Viewer Flask application (light theme variant)
- Template: Japanese minimalist aesthetic
