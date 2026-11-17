/** 品牌标记：登录页与应用内标题旁共用，尺寸可调 */
export const BrandMark = ({ size = 40 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 40 40" fill="none" aria-hidden="true">
    <rect width="40" height="40" rx="12" fill="var(--dsw-alias-brand-primary)" />
    <path
      d="M12 27V13h7.2c4.3 0 7.1 2.6 7.1 6.8 0 4.3-2.9 7.2-7.3 7.2H12Zm3.7-3.2h3.2c2.4 0 3.9-1.5 3.9-3.9 0-2.4-1.5-3.9-3.9-3.9h-3.2v7.8Z"
      fill="var(--dsw-alias-label-primary-foreground)"
    />
    <circle
      cx="30.5"
      cy="27.5"
      r="2.5"
      fill="var(--dsw-alias-label-primary-foreground)"
      opacity="0.65"
    />
  </svg>
)
