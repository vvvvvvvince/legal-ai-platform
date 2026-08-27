type Props = {
  thinking?: boolean;
};

export function LegalAssistantMark({ thinking = false }: Props) {
  return (
    <span
      className={`legal-assistant-mark${thinking ? " legal-assistant-mark-thinking" : ""}`}
      aria-hidden="true"
    >
      <span>律</span>
    </span>
  );
}
