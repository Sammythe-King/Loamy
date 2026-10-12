import { useEffect, useState } from "react";
import { bankInitials, resolveBankLogoSources } from "../utils/bankLogo";

const GENERIC_FAVICON = /google\.com\/s2\/favicons/;

/*
 * BankLogo - official bank logo resolved from the bank name.
 * Tries each resolved source in order, then falls back to initials. A backend
 * logo is only used when the name is unknown and it is not a Google favicon
 * (those return a generic globe for banks they cannot find).
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
    />
  );
};

export default BankLogo;
