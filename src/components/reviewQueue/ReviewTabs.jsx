export const REVIEW_TABS = [
  { id: "expenses", label: "Expenses" },
  { id: "transfers", label: "Internal Transfers" },
  { id: "activity", label: "System Activity" },
];

const ReviewTabs = ({ active, onChange, pending }) => (
  <div className="rqt-tabs" role="tablist" aria-label="Review queue sections">
    {REVIEW_TABS.map((tab) => {
      const count = pending[tab.id] || 0;
      const selected = active === tab.id;
      return (
        <button
          key={tab.id}
          id={`rqt-tab-${tab.id}`}
          type="button"
          role="tab"
          aria-selected={selected}
          aria-controls={`rqt-panel-${tab.id}`}
          tabIndex={selected ? 0 : -1}
          className={`rqt-tab ${selected ? "is-active" : ""}`}
          onClick={() => onChange(tab.id)}
        >
          {tab.label}
          {count > 0 && (
            <span className="rqt-dot">
              <span className="sr-only">{count} pending</span>
            </span>
          )}
        </button>
      );
    })}
  </div>
);

export default ReviewTabs;
