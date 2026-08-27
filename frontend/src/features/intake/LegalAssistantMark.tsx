type Props = {
  thinking?: boolean;
};

export function LegalAssistantMark({ thinking = false }: Props) {
  return (
    <span
      className={`legal-assistant-mark${thinking ? " legal-assistant-mark-thinking" : ""}`}
      aria-hidden="true"
    >
      <img className="legal-assistant-robot-image" src="/assets/legal-assistant-bot-v1.png" alt="" />
    </span>
  );
}
