Generated from the saved MCP call results in E11_calls/ (fixture home with tables, port 18810) and E11_calls_agenthome/ (fixture home built by the agent from the mock site, port 18811). FIXTURE data, not real documents.

| tool | home | args | result had `provenance` | `page`/`caption` keys | warnings key (non-empty?) | paywall keys | disclaimer keys | note |
|---|---|---|---|---|---|---|---|---|
| get_overview | tables | `{}` | no | – | open_warnings (empty/absent) | – | disclaimers | |
| list_entries | tables | `{}` | no | – | entries.open_warnings (empty/absent) | entries.paywalled | – | |
| get_entry_metadata | tables | `{"entry": "standard:1"}` | no | – | warnings (empty/absent) | paywalled | disclaimers | |
| get_entry_metadata | tables | `{"entry": "standard:2"}` | no | – | warnings (non-empty) | paywalled | disclaimers | |
| get_warnings | tables | `{}` | no | – | warnings (non-empty) | – | – | |
| get_disclaimers | tables | `{}` | no | – | – (empty/absent) | – | disclaimers | |
| graph_summary | tables | `{"ref": "aaaaaaaaaaaaaaaa"}` | yes | – | – (empty/absent) | – | provenance.disclaimers | |
| get_outline | tables | `{"ref": "aaaaaaaaaaaaaaaa"}` | yes | outline.page | – (empty/absent) | – | provenance.disclaimers | |
| search_graph | tables | `{"ref": "aaaaaaaaaaaaaaaa", "query": "damping"}` | yes | – | – (empty/absent) | – | provenance.disclaimers | |
| get_node | tables | `{"ref": "aaaaaaaaaaaaaaaa", "node_id": "h0002"}` | yes | – | – (empty/absent) | – | provenance.disclaimers | |
| get_neighbors | tables | `{"ref": "aaaaaaaaaaaaaaaa", "node_id": "h0002"}` | yes | – | – (empty/absent) | – | provenance.disclaimers | |
| get_subgraph | tables | `{"ref": "aaaaaaaaaaaaaaaa", "center": "h0002"}` | yes | – | – (empty/absent) | – | provenance.disclaimers | |
| get_table | tables | `{"ref": "aaaaaaaaaaaaaaaa", "node_id": "tbl0008"` | yes | page | – (empty/absent) | – | provenance.disclaimers | |
| list_assets | tables | `{"ref": "standard:1"}` | yes | documents.assets.caption, documents.assets.page | documents.warnings, provenance.warnings (empty/absent) | documents.paywalled, paywalled_assets_policy | provenance.disclaimer | |
| list_assets | tables | `{"ref": "standard:2"}` | yes | documents.assets.caption, documents.assets.page | documents.warnings, provenance.warnings (non-empty) | documents.paywalled, paywalled_assets_policy | provenance.disclaimer | |
| get_figure | tables | `{"ref": "aaaaaaaaaaaaaaaa", "asset_id": "aaaaaaa` | NOT RUN/ERROR | | | | | ["Error executing tool get_figure: no figure 'aaaaaaaaaaaaaaaa/fig0001'"] |
| get_table_csv | tables | `{"ref": "aaaaaaaaaaaaaaaa", "asset_id": "aaaaaaa` | yes | caption, page | warnings (empty/absent) | paywalled | provenance.disclaimers | |
| get_table_csv | tables | `{"ref": "bbbbbbbbbbbbbbbb", "asset_id": "bbbbbbb` | yes | caption, page | warnings (non-empty) | paywalled | provenance.disclaimers | |
| list_profiles | tables | `{}` | no | – | – (empty/absent) | – | – | |
| set_session_preferences | tables | `{"profile": "strict", "session_id": "ev-strict"}` | no | – | – (empty/absent) | – | – | |
| get_session_preferences | tables | `{"session_id": "ev-strict"}` | no | – | – (empty/absent) | – | – | |
| set_session_preferences | tables | `{"profile": "exploratory", "session_id": "ev-exp` | no | – | – (empty/absent) | – | – | |
| get_session_preferences | tables | `{"session_id": "ev-expl"}` | no | – | – (empty/absent) | – | – | |
| reset_session_preferences | tables | `{"session_id": "ev-expl"}` | no | – | – (empty/absent) | – | – | |
| get_data_dictionary | tables | `{"ref": "aaaaaaaaaaaaaaaa"}` | NOT RUN/ERROR | | | | | ["Error executing tool get_data_dictionary: no data dictionary / relationship map for 'aaaaaaaaaaaaaaaa': none |
| get_relationship_map | tables | `{"ref": "aaaaaaaaaaaaaaaa"}` | NOT RUN/ERROR | | | | | ["Error executing tool get_relationship_map: no data dictionary / relationship map for 'aaaaaaaaaaaaaaaa': non |
| get_join_evaluation | tables | `{"ref": "aaaaaaaaaaaaaaaa"}` | NOT RUN/ERROR | | | | | ["Error executing tool get_join_evaluation: no data dictionary / relationship map for 'aaaaaaaaaaaaaaaa': none |
| list_tables | tables | `{}` | NOT RUN/ERROR | | | | | ['Error executing tool list_tables: table database not reachable: OperationalError: connection failed: connect |
| describe_table | tables | `{"table_id": "aaaaaaaaaaaaaaaa/tbl0008"}` | NOT RUN/ERROR | | | | | ['Error executing tool describe_table: table database not reachable: OperationalError: connection failed: conn |
| explain_units | tables | `{"table_id": "cccccccccccccccc/tbl0004"}` | NOT RUN/ERROR | | | | | ['Error executing tool explain_units: table database not reachable: OperationalError: connection failed: conne |
| search_tables | tables | `{"question": "damping"}` | NOT RUN/ERROR | | | | | ['Error executing tool search_tables: table database not reachable: OperationalError: connection failed: conne |
| lookup_values | tables | `{"question": "allowable stress A572", "session_i` | NOT RUN/ERROR | | | | | ['Error executing tool lookup_values: table database not reachable: OperationalError: connection failed: conne |
| query_table | tables | `{"table_id": "aaaaaaaaaaaaaaaa/tbl0008"}` | NOT RUN/ERROR | | | | | ['Error executing tool query_table: table database not reachable: OperationalError: connection failed: connect |
| join_tables | tables | `{"left_table": "aaaaaaaaaaaaaaaa/tbl0004", "righ` | NOT RUN/ERROR | | | | | ['Error executing tool join_tables: table database not reachable: OperationalError: connection failed: connect |
| evaluate_joins | tables | `{"ref": "standard:1"}` | NOT RUN/ERROR | | | | | ['Error executing tool evaluate_joins: table database not reachable: OperationalError: connection failed: conn |
| run_sql | tables | `{"query": "select 1"}` | NOT RUN/ERROR | | | | | ['Error executing tool run_sql: table database not reachable: OperationalError: connection failed: connection  |
| export_graph | tables | `{"ref": "aaaaaaaaaaaaaaaa", "max_nodes": 20}` | yes | graph.edges.page | – (empty/absent) | – | provenance.disclaimers | |
| get_overview | agenthome | `{}` | no | – | agent_last_run_summary.forced_warnings, agent_last_run_summary.warnings, open_warnings (empty/absent) | – | disclaimers | |
| list_entries | agenthome | `{}` | no | – | entries.open_warnings (empty/absent) | entries.paywalled | – | |
| get_entry_metadata | agenthome | `{"entry": "standard:2"}` | no | – | warnings (empty/absent) | paywalled | disclaimers | |
| get_dependents | agenthome | `{"entry": "standard:1"}` | no | – | – (empty/absent) | – | – | |
| get_warnings | agenthome | `{}` | no | – | warnings (empty/absent) | – | – | |
| graph_summary | agenthome | `{"ref": "standard:1"}` | yes | – | provenance.warnings (empty/absent) | – | provenance.disclaimer | |
| graph_summary | agenthome | `{"ref": "combined"}` | yes | – | – (empty/absent) | – | provenance.disclaimer | |
| get_outline | agenthome | `{"ref": "standard:1"}` | yes | outline.page | provenance.warnings (empty/absent) | – | provenance.disclaimer | |
| search_graph | agenthome | `{"ref": "standard:1", "query": "50.69"}` | yes | – | provenance.warnings (empty/absent) | – | provenance.disclaimer | |
| get_node | agenthome | `{"ref": "combined", "node_id": "entry:standard:1` | yes | – | – (empty/absent) | node.paywalled | provenance.disclaimer | |
| get_neighbors | agenthome | `{"ref": "combined", "node_id": "entry:standard:1` | yes | – | – (empty/absent) | neighbors.node.paywalled | provenance.disclaimer | |
| get_subgraph | agenthome | `{"ref": "combined", "center": "entry:standard:1"` | yes | – | – (empty/absent) | nodes.paywalled | provenance.disclaimer | |
| list_assets | agenthome | `{"ref": "standard:1"}` | yes | – | documents.warnings, provenance.warnings (empty/absent) | documents.paywalled, paywalled_assets_policy | provenance.disclaimer | |
| export_graph | agenthome | `{"ref": "combined", "max_nodes": 20}` | yes | – | – (empty/absent) | graph.nodes.paywalled | provenance.disclaimer | |
