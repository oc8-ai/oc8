# Costs

**Route:** `/costs`

## What it is

Token / model **spend visibility** across agents and departments — watched from
the main sidebar (not buried only under Settings).

## What it is for

- See who spends what
- Set or review limits
- Catch runaway agents early

## Where you are in the flow

```text
Agents run models → ★ Costs (monitor) → adjust models / limits / autonomy
```

Costs do not approve work; they inform budget and model choices in
[Models](models.md) and agent configuration.

## Tabs

| Tab | Purpose |
|-----|---------|
| **Overview** | Totals and trends |
| **By Agent** | Spend per worker |
| **By Department** | Spend per team |
| **Per employee** | Tokens per person over time |
| **Limits** | Caps and thresholds |

## Per employee

A stacked area chart of the tokens (input + output) each person used, with a
totals table for the range below it.

- **Ranges:** 24 h and 7 days in hourly steps, 30 days in daily steps
  (default 7 days). Steps are cut in the organisation's time zone, shown
  under the heading; the last step contains "now".
- **Who is counted:** usage counts for the person a chat belongs to, the
  person an agent run acts for, or the member calling the LLM gateway.
- **Top 10:** the ten people with the most tokens in the range get their own
  series; everyone else is summed up as **Others**.
- **No person:** usage no person is attached to — anything recorded before
  version 0.20.0, and usage without a person such as embeddings and
  knowledge-base indexing.
- **Unknown person:** a member whose account was removed or has no display
  name; their usage still shows.
- **Access:** needs the `budget:view` permission, like the rest of Costs.

## What you do here

1. Review Overview after a pilot week.
2. Drill into expensive agents; open them to change model or autonomy.
3. Set limits appropriate for your organisation.

## Where work goes next

| Finding | Next |
|---------|------|
| One agent too expensive | [Agents](agents.md) → cheaper model / fewer runs |
| Need another provider | [Models](models.md) |
| Unexpected tool loops | [Activity](activity.md) + Live Log |

## Related

- [Models](models.md)
- [Agents](agents.md)
