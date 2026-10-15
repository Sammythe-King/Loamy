import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { faChevronLeft, faChevronRight } from "@fortawesome/free-solid-svg-icons";

export const paginate = (list, page, pageSize) => {
  const totalPages = Math.max(1, Math.ceil(list.length / pageSize));
  const safePage = Math.min(Math.max(page, 1), totalPages);
  const start = (safePage - 1) * pageSize;
  return {
    pageItems: list.slice(start, start + pageSize),
    page: safePage,
    totalPages,
    start,
    end: Math.min(start + pageSize, list.length),
    total: list.length,
  };
};

const Pagination = ({ page, totalPages, start, end, total, onChange, label }) => {
  if (totalPages <= 1) return null;
  return (
    <nav className="rqt-pager" aria-label={`${label} pages`}>
      <span className="rqt-pager-info">
        Showing {start + 1}&ndash;{end} of {total}
      </span>
      <div className="rqt-pager-controls">
        <button
          type="button"
          className="rqt-secondary rqt-pager-btn"
          onClick={() => onChange(page - 1)}
          disabled={page <= 1}
        >
          <FontAwesomeIcon icon={faChevronLeft} />
          Previous
        </button>
        <span className="rqt-pager-page" aria-current="page">
          Page {page} of {totalPages}
        </span>
        <button
          type="button"
          className="rqt-secondary rqt-pager-btn"
          onClick={() => onChange(page + 1)}
          disabled={page >= totalPages}
        >
          Next
          <FontAwesomeIcon icon={faChevronRight} />
        </button>
      </div>
    </nav>
  );
};

export default Pagination;
