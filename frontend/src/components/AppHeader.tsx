// App header with Rapid7 logo + product lock-up (brand guidelines).
//
// The Rapid7 logo appears first, separated from the product name by a vertical
// rule whose width is "half the width of the 'i' in insight" per the brand
// lock-up spec. The product name uses the heading font in uppercase.
//
// When `onBack` is provided (i.e. a session is active), a "Menu" button appears
// on the right side to navigate back to the entry-mode selector.

import rapid7Logo from "../assets/rapid7-logo.png";

export interface AppHeaderProps {
  /** When provided, renders a back-to-menu button on the right. */
  onBack?: () => void;
}

export function AppHeader({ onBack }: AppHeaderProps) {
  return (
    <header className="app-header" role="banner">
      <div className="app-header__lockup">
        <img
          src={rapid7Logo}
          alt="Rapid7"
          className="app-header__logo"
          height={28}
        />
        <span className="app-header__divider" aria-hidden="true" />
        <span className="app-header__product">InsightConnect Plugin Builder</span>
      </div>
      {onBack && (
        <button
          type="button"
          className="app-header__back"
          onClick={onBack}
          aria-label="Back to menu"
        >
          &larr; Menu
        </button>
      )}
    </header>
  );
}
