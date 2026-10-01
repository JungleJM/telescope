# Format test (.md)

Every way I know to make "Claude" and "Your response" look different. Each option has a number: tell me which ones show as coloured or clearly separate in your viewer. Open with VS Code's preview (Cmd+Shift+V), and also try saving the file once in your editor, then check whether it rewrote any option (as it did with `\[!NOTE\]`).

------------------------------------------------------------------------

## 1. GitHub alerts, all five kinds

> \[!NOTE\] **Claude:** a NOTE alert (blue on GitHub).

> \[!TIP\] **Your response:** a TIP alert (green).

> \[!IMPORTANT\] **Your response:** an IMPORTANT alert (purple).

> \[!WARNING\] **Your response:** a WARNING alert (orange).

> \[!CAUTION\] **Your response:** a CAUTION alert (red).

## 2. Plain blockquote with an emoji label

> 🟦 **Claude:** a plain quote block, labelled with an emoji.

> 🟧 **Your response:** the same, with another emoji.

## 3. Nested blockquote (your reply indented further)

> 🟦 **Claude:** the outer quote.
>
> > 🟧 **Your response:** a quote inside the quote.

## 4. HTML box with a background colour

::: {style="background-color:#1e3a5f; border-left:6px solid #4a90e2; padding:8px 12px; margin:8px 0;"}
<b>Claude:</b> an HTML div with a blue background and border.
:::

::: {style="background-color:#5f3a1e; border-left:6px solid #e2904a; padding:8px 12px; margin:8px 0;"}
<b>Your response:</b> the same, orange.
:::

## 5. HTML box with a border only (survives light and dark themes)

::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}
<b>Claude:</b> a blue outline.
:::

::: {style="border:2px solid #e2904a; border-radius:6px; padding:8px 12px; margin:8px 0;"}
<b>Your response:</b> an orange outline.
:::

## 6. Coloured text only

[<b>Claude:</b> blue text in a span.]{style="color:#4a90e2"}

[<b>Your response:</b> orange text in a span.]{style="color:#e2904a"}

## 7. LaTeX colour (math rendering)

$\color{#4a90e2}{\textbf{Claude:}}$ a coloured label drawn by the math renderer, then ordinary text.

$\color{#e2904a}{\textbf{Your response:}}$ the same, orange.

## 8. Highlighted label

<mark><b>Claude:</b></mark> a highlighted label.

<mark style="background-color:#e2904a"><b>Your response:</b></mark> an orange highlight.

## 9. One-cell table as a box

| 🟦 Claude                                                     |
|---------------------------------------------------------------|
| A table with one column: the header bar sets the reply apart. |

| 🟧 Your response            |
|-----------------------------|
| Your text goes in the cell. |

## 10. Collapsible block

<details open>

<summary><b>🟦 Claude</b></summary>

A collapsible section, open by default. Markdown works inside if there is a blank line after the summary.

</details>

<details open>

<summary><b>🟧 Your response</b></summary>

Your text here.

</details>

## 11. Code block, `diff` (green and red lines)

``` diff
+ Claude: lines starting with + show green in most themes.
- Your response: lines starting with - show red.
```

## 12. Code block, plain text (for comparison)

``` text
Claude: monospace, no bold or lists, no wrapping.
```

## 13. Heading per reply

#### 🟦 Claude

A small heading above each reply.

#### 🟧 Your response

Your text here.

## 14. Rule and label

---
**🟦 Claude** · a horizontal rule above each reply.
---

**🟧 Your response** · the same.

## 15. MkDocs admonition (shows whether your viewer has the extension)

!!! note "Claude" An MkDocs-style admonition.

!!! warning "Your response" The same, warning style.