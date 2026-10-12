import { useEffect, useState } from "react";
import { bankInitials, resolveBankLogoSources } from "../utils/bankLogo";

const GENERIC_FAVICON = /(google\.com|googleusercontent\.com)\/s2\/favicons/;
const PLACEHOLDER_MAX_WIDTH = 16;

/*
 * BankLogo - bank logo resolved from the bank name via the Google favicon service.
 * Tries each resolved source in order (skipping load errors and Google's 16px
 * placeholder, which it serves with a 404 that browsers still render), then
 * falls back to initials. A backend logo is only used
 * when the name is unknown and it is not a Google favicon.
 */
const BankLogo = ({ name, slug, backendLogo, className = "", imgClassName = "", size = 28 }) => {
  const resolved = resolveBankLogoSources(name, slug);
  const sources = resolved.length === 0 && backendLogo && !GENERIC_FAVICON.test(backendLogo) ? [backendLogo] : resolved;
  const key = sources.join("|");
  const [index, setIndex] = useState(0);

  useEffect(() => setIndex(0), [key]);

  const src = sources[index];
  if (!src) {
    return (
      <span className={`bank-logo-initials ${className}`} style={{ width: size, height: size }} aria-hidden="true">
        {bankInitials(name)}
      </span>
    );
  }
  return (
    <img
      key={src}
      src={src}
      alt=""
      className={imgClassName}
      width={size}
      height={size}
      loading="lazy"
      referrerPolicy="no-referrer"
      onError={() => setIndex((i) => i + 1)}
      onLoad={(event) => {
        if (event.currentTarget.naturalWidth <= PLACEHOLDER_MAX_WIDTH) setIndex((i) => i + 1);
      }}
    />
  );
};

export default BankLogo;
