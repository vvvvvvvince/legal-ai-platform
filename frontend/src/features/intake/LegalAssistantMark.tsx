type Props = {
  thinking?: boolean;
};

export function LegalAssistantMark({ thinking = false }: Props) {
  return (
    <span
      className={`legal-assistant-mark${thinking ? " legal-assistant-mark-thinking" : ""}`}
      aria-hidden="true"
    >
      <span className="legal-assistant-robot-antenna" />
      <span className="legal-assistant-robot-ear legal-assistant-robot-ear-left" />
      <span className="legal-assistant-robot-head">
        <span className="legal-assistant-robot-eyes">
          <span className="legal-assistant-robot-eye" />
          <span className="legal-assistant-robot-eye" />
        </span>
        <span className="legal-assistant-robot-status" />
      </span>
      <span className="legal-assistant-robot-ear legal-assistant-robot-ear-right" />
    </span>
  );
}
