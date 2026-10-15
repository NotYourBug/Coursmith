---
name: "Coursmith · 智课工坊"
description: "Quiet, bookish learning and a clear operations workspace."
colors:
  green: "#234b3e"
  green-hover: "#315f4a"
  public-bg: "#f7f8f4"
  paper: "#fff"
  public-ink: "#202c27"
  public-muted: "#626d65"
  public-line: "#dde3dc"
  public-soft: "#eaf0e8"
  public-field-line: "#c4cec1"
  field-bg: "#fcfdfb"
  public-focus: "#487254"
  outline-line: "#b9c8b8"
  public-tag-bg: "#eef2eb"
  public-tag-ink: "#3e5c46"
  admin-bg: "#f5f7f3"
  admin-ink: "#25322b"
  admin-muted: "#667367"
  admin-line: "#dfe5dc"
  admin-soft: "#e9f0e5"
  admin-soft-hover: "#dde9d6"
  admin-field-line: "#c8d2c4"
  admin-field-hover: "#92ab8c"
  admin-focus: "#4c7955"
  sidebar-bg: "#fafbf8"
  nav-ink: "#536154"
  nav-hover-bg: "#edf1e9"
  nav-active-bg: "#e2eddb"
  nav-active-ink: "#22472e"
  admin-tag-bg: "#e9f2e4"
  admin-tag-ink: "#3b623a"
  table-head-bg: "#f0f4ec"
  table-head-ink: "#647360"
  table-row-line: "#e7ece3"
  table-row-hover: "#fafcf7"
  error-bg: "#faf0ed"
  error-ink: "#8d352a"
  public-error-line: "#e9c9c2"
  admin-error-line: "#e8c9c3"
  status-bg: "#edf4e7"
  status-ink: "#36522c"
  status-line: "#cbdcbd"
  selection-bg: "#d6e7c9"
  selection-ink: "#173c2e"
typography:
  display:
    fontFamily: '"Noto Serif SC", "Songti SC", "STSong", "SimSun", serif'
    fontSize: "clamp(2.2rem, 3.8vw, 3.65rem)"
    fontWeight: 700
    lineHeight: 1.45
    letterSpacing: "-.035em"
  public-headline:
    fontFamily: '"Noto Serif SC", "Songti SC", "STSong", "SimSun", serif'
    fontSize: "clamp(1.8rem, 4vw, 2.8rem)"
    fontWeight: 650
    lineHeight: 1.45
  public-section:
    fontFamily: '"Manrope", "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "1.5rem"
    fontWeight: 650
    lineHeight: 1.45
    letterSpacing: "-.025em"
  public-title:
    fontFamily: '"Manrope", "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "1.15rem"
    fontWeight: 650
    lineHeight: 1.45
  body:
    fontFamily: '"Manrope", "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.8
  admin-headline:
    fontFamily: '"Manrope", "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "1.9rem"
    fontWeight: 650
    lineHeight: 1.4
    letterSpacing: "-.025em"
  admin-title:
    fontFamily: '"Manrope", "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "1.18rem"
    fontWeight: 650
    lineHeight: 1.4
    letterSpacing: "-.025em"
  admin-body:
    fontFamily: '"Manrope", "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.75
  button:
    fontFamily: '"Manrope", "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: ".9rem"
    fontWeight: 600
    lineHeight: 1.5
  admin-button:
    fontFamily: '"Manrope", "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: ".82rem"
    fontWeight: 600
    lineHeight: 1.5
  editor:
    fontFamily: 'ui-monospace, "Microsoft YaHei", monospace'
    fontSize: ".81rem"
    fontWeight: 400
    lineHeight: 1.9
rounded:
  badge: "4px"
  field: "6px"
  public-button: "7px"
  table: "8px"
  admin-form: "9px"
  card: "10px"
  auth: "12px"
spacing:
  xs: "6px"
  sm: "8px"
  field-gap: "10px"
  inline: "12px"
  form-gap: "14px"
  compact: "16px"
  mobile-inset: "18px"
  row: "20px"
  grid: "24px"
  panel: "26px"
  tablet-inset: "28px"
  section: "32px"
  invitation: "36px"
  form: "40px"
  desktop-inset: "42px"
  detail-gap: "64px"
components:
  button-primary:
    backgroundColor: "{colors.green}"
    textColor: "{colors.paper}"
    typography: "{typography.button}"
    rounded: "{rounded.public-button}"
    padding: "11px 21px"
  button-primary-hover:
    backgroundColor: "{colors.green-hover}"
  button-outline:
    backgroundColor: "transparent"
    textColor: "{colors.green}"
    typography: "{typography.button}"
    rounded: "{rounded.public-button}"
    padding: "11px 21px"
  button-admin:
    backgroundColor: "{colors.green}"
    textColor: "{colors.paper}"
    typography: "{typography.admin-button}"
    rounded: "{rounded.field}"
    padding: "10px 18px"
  button-admin-secondary:
    backgroundColor: "{colors.admin-soft}"
    textColor: "{colors.green}"
    typography: "{typography.admin-button}"
    rounded: "{rounded.field}"
    padding: "10px 18px"
  input-public:
    backgroundColor: "{colors.field-bg}"
    textColor: "{colors.public-ink}"
    typography: "{typography.body}"
    rounded: "{rounded.field}"
    padding: "11px 13px"
  nav-admin-active:
    backgroundColor: "{colors.nav-active-bg}"
    textColor: "{colors.nav-active-ink}"
    rounded: "{rounded.public-button}"
    padding: "10px 14px"
  course-card:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.public-ink}"
    rounded: "{rounded.card}"
  status-badge:
    backgroundColor: "{colors.admin-tag-bg}"
    textColor: "{colors.admin-tag-ink}"
    rounded: "{rounded.badge}"
    padding: "2px 8px"
  editor-panel:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.admin-ink}"
    rounded: "{rounded.admin-form}"
    padding: "24px"
---

# Design System: Coursmith · 智课工坊

## Overview

**Creative North Star: "Quiet reading, clear making"**

Coursmith pairs a calm, bookish learning website with a practical operations workspace. Paper-white grounds, dark green actions, and restrained borders give real course content and production tasks room to breathe. The book motif is a webpage graphic made from layered shapes and line drawings.

Chinese serif headlines distinguish learning and introductory moments. The denser administrative interface uses sans-serif hierarchy, grouped navigation, readable forms, and tables. Shared colors and local fonts connect the surfaces while their density remains appropriate to their work.

**Key Characteristics:**
- Paper-white surfaces, green actions, and quiet ink text.
- Serif learning headlines with sans-serif interface text.
- Bordered panels and tonal grouping, with selective soft shadows.
- Explicit actions, visible keyboard focus, and responsive native navigation.

The implementation is code-led; there is no approved image comp. The frontmatter records reused values from the final stylesheets. Component states, responsive measurements, shadows, and motion extend it in `.impeccable/design.json`.

## Colors

The palette combines muted greens with lightly tinted whites; the learning and operations surfaces use separate neutral sets.

### Primary
- **Quiet Green:** links, primary actions, active categories, and the book cover. The deeper interaction treatment uses `green-hover`.
- **Soft Green:** public supporting actions and callouts use `public-soft`; operations secondary actions use `admin-soft` and its hover tone.

### Neutral
- **Reading Paper:** `public-bg` grounds the learning website; `paper` contains course information, lessons, and forms.
- **Workspace Paper:** `admin-bg`, `sidebar-bg`, and `field-bg` distinguish the operations canvas, navigation, and fields.
- **Ink and Muted Ink:** use each surface's corresponding ink and muted tokens for hierarchy. Corresponding line tokens separate content without heavy frames.

Error and status colors describe actual feedback. Red-tinted alerts, green-tinted status messages, and textual badges retain their meaning in words. Text selection uses the same pale green and dark ink on both surfaces.

**The Surface Neutrals Rule.** Keep the public and operations neutral sets with their respective surfaces; share the green action color.

## Typography

**Display Font:** self-hosted Noto Serif SC with Chinese serif fallbacks. The public stack includes Songti SC, STSong, and SimSun; administrative introductory serif sections use Songti SC and SimSun.

**Body Font:** self-hosted Manrope with PingFang SC and Microsoft YaHei. Manrope supplies Latin text and numbers; Chinese interface text relies on the Chinese sans-serif fallbacks. Both font faces use `font-display: swap`. Manrope declares weights (400–700); Noto Serif SC declares (600–700).

**Label/Mono Font:** native monospace for code; the Markdown editor also includes Microsoft YaHei in its monospace stack.

**Character:** generous Chinese line spacing and measured serif headlines coexist with compact interface labels and tabular numbers. Noto Serif SC is a partial display subset, not a complete Chinese font: uncovered course titles and other characters use the listed fallbacks.

### Hierarchy
- **Display:** the homepage uses `display`; at widths up to (700px), it changes to `clamp(2rem, 6.9vw, 2.8rem)`, then (1.9rem) at widths up to (390px).
- **Headline:** ordinary public page headings use `public-headline`; course detail headings use (700), `clamp(1.9rem, 4vw, 3rem)`, and (1.4) line height. Administrative headings use `admin-headline`, reducing to (1.65rem) at widths up to (600px).
- **Title:** ordinary public section headings use `public-section`; the homepage course-section heading reduces to (1.28rem) at widths up to (700px). Course cards use `public-title`, reducing to (1.05rem) at widths up to (700px). Administrative section headings use `admin-title`, reducing to (1.1rem) at widths up to (600px).
- **Body:** public text uses `body`, reducing to (14px) at widths up to (700px). Lesson paragraphs use (1.95) line height and a maximum of (75ch); administrative paragraphs allow (85ch).
- **Label:** public form labels use (.85rem), weight (600); operations labels use (.8rem), weight (550). Status badges use (.7rem); tables use (.8rem) with tabular numbers.

## Layout

The public header, main shell, and footer share a centered maximum width (1180px) with total horizontal subtraction (96px). This becomes (56px) at widths up to (1000px), and (40px) at widths up to (700px). The homepage hero uses (1.25fr / 1fr) columns and a (30px) gap; at (700px) it stacks copy and actions before the book graphic. Course cards use two columns with a (24px) gap, then one column at (1000px). At desktop widths from (1100px), a lone card has a maximum grid track of (720px).

Course details pair flexible content with a (310px) sticky aside and a (64px) gap. At (1000px), the aside becomes (280px) and the gap (30px); at (700px), content stacks and the aside becomes static. Reading uses a separate centered shell up to (900px), with total horizontal subtraction (48px), reducing to (28px) at (700px). Long code and tables scroll within their content containers.

The operations sidebar is fixed at (238px), reducing to (215px) at widths up to (1100px). The workspace offsets by the same amount; main content is capped at (1400px), with desktop horizontal padding (42px). At (900px), the sidebar moves into document flow and the workspace offset disappears. Its native disclosure is rendered open so navigation remains available without JavaScript. `shell.js` collapses it on mobile arrival and when crossing into the mobile breakpoint; desktop navigation stays visible. Mobile navigation links retain a two-column grid. At (600px), dashboard and login columns stack, filters stack, and main horizontal padding becomes (18px).

The production editor uses equal columns with a (24px) gap, stacking at widths up to (1050px). Other production columns stack at (700px); editor panels reduce padding from (24px) to (18px). Desktop title forms use two columns from (1100px), with the first label spanning both.

## Elevation & Depth

Borders and pale tonal surfaces establish everyday structure. Soft shadows lift public authentication forms and course cards on hover, with a separate subtle login-panel shadow. The layered, slightly rotated book has its own shadow; that illustration does not define the shape or depth of normal interface panels.

### Shadow Vocabulary
- **Public surface** (`0 14px 32px rgba(35, 65, 44, .07)`): authentication forms and course-card hover.
- **Public action hover** (`0 5px 14px rgba(35, 75, 62, .12)`): button hover.
- **Operations action hover** (`0 3px 9px rgba(35,75,62,.09)`): button hover.
- **Operations login** (`0 18px 48px rgba(44,70,40,.06)`): login container.
- **Book illustration** (`10px 19px 26px rgba(35, 59, 39, .16)`): the homepage book cover.

## Shapes

Corners remain gently rounded. Badge and inline code corners use `badge`; inputs and operations buttons use `field`; public buttons and operations navigation use `public-button`. Tables and lesson panels use `table`, operations forms and editor panels use `admin-form`, course and dashboard cards use `card`, and authentication containers use `auth`. Panels normally have a (1px) border.

The book cover has asymmetric corners (`3px 10px 10px 3px`), a thicker spine, and layered rotated pages. Its pale backdrop has arched upper corners (`45% 45% 9px 9px`). Small circular dots are confined to captions and status indications in the current implementation.

## Components

### Buttons

Clear, compact actions. Public primary buttons use `button-primary`, a minimum height (46px), and a (24px) icon gap. Operations primary buttons use `button-admin`, a minimum height (44px), and a (9px) gap. Outline public actions are transparent with the outline line token; hover adds the public soft fill. Operations secondary actions use the operations soft fill and line, shifting to `admin-soft-hover` on hover.

Primary hover changes the green fill and adds the corresponding soft action shadow. Activation moves buttons down (1px). Disabled buttons dim to (.5) publicly and (.45) in operations; operations also remove their shadow. Keyboard focus uses a (2px) outline with (4px) offset, using the relevant focus color. Ordinary state transitions take (.2s).

### Chips

Public category filters are rectangular rounded links, with padding (8px 17px), minimum height (40px), and solid green selection. Public informational tags use the public tag tokens, padding (3px 8px), and badge corners. Operations status badges use `status-badge`. Status text remains visible; color supplements the label.

### Cards / Containers

Public course cards pair a graphic cover and text on a white bordered panel. The default cover track is (36%), becoming (30%) at (700px); body padding reduces from (26px) to (20px). Hover shifts the card up (3px), adds the public surface shadow, and changes its border to `#b5c5b1`. Alternating graphic covers use a pale warm variation already present in code; this is a card-illustration treatment rather than a second action palette.

Operations dashboard panels have card corners and panel padding; forms use admin-form corners and panel padding. At (600px), dashboard padding becomes (20px), and form padding (18px). Table containers scroll horizontally; headers, row boundaries, and row hover use the corresponding table tokens.

### Inputs / Fields

Fields use `field-bg`, each surface's field line, and field corners with padding (11px 13px). Public fields are at least (48px) high; operations fields at least (44px). Operations field hover changes the line to `admin-field-hover`; focus makes the background white, in addition to the visible focus outline. Textareas resize vertically. Checkboxes and radios retain native controls at (18px) with green accent.

Alerts are distinct bordered blocks: public padding (14px), operations padding (16px 20px). Operations success/status feedback uses padding (14px 18px). Labels and feedback remain associated with real server state; recovery screens present submitted input as unsaved and retain old revisions.

### Navigation

Public navigation uses text links with minimum height (44px), a bottom inset line for the current ordinary page, and a bordered redemption link. Operations links have public-button corners, padding (10px 14px), and minimum height (44px). The selected link uses active navigation tones and weight (650); hover uses `nav-hover-bg`. Navigation groups and inline SVG line icons clarify the workspace structure. Mobile disclosure behavior follows Layout.

### Saved-content Editor

The source and saved preview sit in separate bordered editor panels. The Markdown field uses `editor`, minimum height (480px), and background `#fbfcf9`. The optional toolbar contains compact white buttons in a pale container, wrapping as needed. It remains hidden until JavaScript enables it. Explicit server save refreshes the preview; the preview describes the saved revision. Forms, CSRF fields, revision fields, and input recovery remain the source of truth. The interface does not imply autosave or completed human review.

The book-arrival animation lasts (.8s), easing with `cubic-bezier(.16,1,.3,1)`. Reduced-motion preference removes animation and transitions and restores automatic scrolling on both surfaces.

**The Saved Preview Rule.** Describe the editor preview as saved content and make saving explicit; retain visible unsaved-input recovery.

## Do's and Don'ts

### Do:
- **Do** use the surface-specific neutral set with the shared green action color.
- **Do** preserve local font loading and Chinese fallback stacks, including fallback for the partial serif subset.
- **Do** retain visible keyboard focus, native controls, native disclosure navigation, and reduced-motion behavior.
- **Do** allow long code and tables to scroll within their containers and stack editor columns at the implemented breakpoint.
- **Do** label saved previews and unsaved recovery accurately, keeping real server form and revision behavior.

### Don't:
- **Don't** imply that the bundled Noto Serif SC subset covers all Chinese characters.
- **Don't** replace textual course or production state with color alone.
- **Don't** reuse the illustration's rotated pages, arched backdrop, or book shadow as the default form or table treatment.
- **Don't** imply autosave, completed human review, or invented course facts through interface copy or state styling.
