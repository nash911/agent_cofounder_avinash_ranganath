import { useId } from "react";
import { filterOptions, type FilterOption } from "../lib/collection.js";
import type { AnyConfig, Row } from "../lib/config-types.js";

export interface FilterBarProps {
  readonly config: AnyConfig;
  readonly rows: readonly Row[];
  readonly query: string;
  readonly activeFilter: string;
  onQuery(value: string): void;
  onFilter(id: string): void;
}

interface ChipsProps {
  readonly label: string;
  readonly options: readonly FilterOption[];
  readonly activeFilter: string;
  onFilter(id: string): void;
}

/** One chip group. Stored values and derived states are separate groups, so
 *  every chip has a unique id and only the pressed one reads as pressed. */
function Chips({ label, options, activeFilter, onFilter }: ChipsProps) {
  if (options.length === 0) return null;
  return (
    <div className="chips" role="group" aria-label={label}>
      {options.map((option) => (
        <button
          key={option.id}
          type="button"
          className={option.id === activeFilter ? "chip chip--on" : "chip"}
          aria-pressed={option.id === activeFilter}
          onClick={() => onFilter(option.id)}
        >
          {`${option.label} (${option.count})`}
        </button>
      ))}
    </div>
  );
}

/** Search plus one chip per filter option. Counts come from the unsearched
 *  row set, so they stay still while the reader types. */
export function FilterBar(props: FilterBarProps) {
  const { config, rows, query, activeFilter, onQuery, onFilter } = props;
  const searchId = useId();
  const searchable = config.search !== false;
  const hasFilters = (config.filters?.length ?? 0) > 0;
  if (!searchable && !hasFilters) return null;
  const searchLabel = `Search ${config.copy.nounPlural}`;
  const options = hasFilters ? filterOptions(config, rows) : [];
  const isState = (option: FilterOption) => option.id.startsWith("state:");

  return (
    <div className="toolbar">
      {searchable ? (
        <>
          <label className="visually-hidden" htmlFor={searchId}>
            {searchLabel}
          </label>
          <input
            id={searchId}
            className="search"
            type="search"
            value={query}
            placeholder={searchLabel}
            onChange={(event) => onQuery(event.target.value)}
          />
        </>
      ) : null}
      <Chips
        label="Filters"
        options={options.filter((option) => !isState(option))}
        activeFilter={activeFilter}
        onFilter={onFilter}
      />
      <Chips
        label="Views"
        options={options.filter(isState)}
        activeFilter={activeFilter}
        onFilter={onFilter}
      />
    </div>
  );
}
