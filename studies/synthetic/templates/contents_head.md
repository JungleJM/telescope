# Contents

**SYNTHETIC DATA.** Every value was generated; nothing comes from a patient or from Cosmos. Every key begins with 7007. This copy holds {{visits}} ED visits (generator seed {{seed}}).

Each table below is one parquet in `data/cosmos_parquets/`, with the columns and SQL types the real pull has. A parquet keeps those types:

| SQL | In the parquet |
|---|---|
| BIGINT | int64 |
| INT | int32 |
| SMALLINT, TINYINT | int16 |
| BIT | bool |
| FLOAT | double |
| NUMERIC(p,s) | decimal(p,s) |
| DATE | date32 |
| DATETIME2 | timestamp[us] |
| NVARCHAR | string |

Columns that the analysis doesn't use are filled with plausible but meaningless values, often `*Unspecified` or blank.
