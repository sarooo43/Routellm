import { motion } from 'framer-motion'
import type { FallbackChainEntry } from '../types/api'

const NODE_R = 18
const NODE_GAP = 110
const GRAPH_HEIGHT = 96
const FIRST_CENTER_X = 30

const SEGMENT_STAGGER_S = 0.3

/**
 * Horizontal flowchart of a request's fallback chain: one node per attempt,
 * connected by lines that draw in sequentially — the visual story of
 * "tried this, failed, tried next, succeeded". The final successful node
 * carries a pulsing glow ring.
 */
export function FallbackChainGraph({ attempts }: { attempts: FallbackChainEntry[] }) {
  if (attempts.length === 0) {
    return <p className="text-sm text-text-secondary/70">No attempts were recorded.</p>
  }

  const centerX = (index: number) => FIRST_CENTER_X + index * NODE_GAP
  const width = centerX(attempts.length - 1) + NODE_R + 20
  const successIndex = attempts.findIndex((attempt) => attempt.success)

  return (
    <svg
      width={width}
      height={GRAPH_HEIGHT}
      viewBox={`0 0 ${width} ${GRAPH_HEIGHT}`}
      className="max-w-full"
      role="img"
      aria-label="Fallback chain"
    >
      {attempts.map((attempt, index) => {
        const color = attempt.success ? '#3DDC97' : '#FF4D4D'
        const cx = centerX(index)
        const isFinalSuccess = index === successIndex

        return (
          <g key={`${attempt.provider_name}-${index}`}>
            {/* Connector line to the NEXT node, drawn in after this node. */}
            {index < attempts.length - 1 && (
              <motion.line
                x1={cx + NODE_R + 4}
                y1={34}
                x2={centerX(index + 1) - NODE_R - 4}
                y2={34}
                stroke="rgba(255, 255, 255, 0.25)"
                strokeWidth="2"
                strokeLinecap="round"
                initial={{ pathLength: 0, opacity: 0 }}
                animate={{ pathLength: 1, opacity: 1 }}
                transition={{ duration: 0.25, delay: index * SEGMENT_STAGGER_S + 0.15 }}
              />
            )}

            {/* Pulsing glow ring around the final successful node. */}
            {isFinalSuccess && (
              <motion.circle
                cx={cx}
                cy={34}
                r={NODE_R}
                fill="none"
                stroke="#3DDC97"
                strokeWidth="2"
                initial={{ opacity: 0.8 }}
                animate={{ r: [NODE_R, NODE_R + 7], opacity: [0.8, 0] }}
                transition={{ duration: 1.6, repeat: Infinity, ease: 'easeOut' }}
              />
            )}

            <motion.circle
              cx={cx}
              cy={34}
              r={NODE_R}
              fill={attempt.success ? 'rgba(61, 220, 151, 0.15)' : 'rgba(255, 77, 77, 0.15)'}
              stroke={color}
              strokeWidth="2"
              initial={{ opacity: 0, scale: 0.6 }}
              animate={{ opacity: 1, scale: 1 }}
              transition={{
                duration: 0.3,
                delay: index * SEGMENT_STAGGER_S,
                type: 'spring',
                stiffness: 300,
                damping: 20,
              }}
              style={{ originX: `${cx}px`, originY: '34px' }}
            />

            <motion.text
              x={cx}
              y={34}
              textAnchor="middle"
              dominantBaseline="central"
              fontSize="13"
              fontWeight="700"
              fill={color}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ delay: index * SEGMENT_STAGGER_S + 0.1 }}
            >
              {attempt.success ? '✓' : '✗'}
            </motion.text>

            <motion.text
              x={cx}
              y={72}
              textAnchor="middle"
              fontSize="11"
              fill="#F5F5F5"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ delay: index * SEGMENT_STAGGER_S + 0.15 }}
            >
              {attempt.provider_name}
            </motion.text>

            <motion.text
              x={cx}
              y={86}
              textAnchor="middle"
              fontSize="10"
              fill="#A0A0A0"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ delay: index * SEGMENT_STAGGER_S + 0.2 }}
            >
              {Math.round(attempt.latency_ms)}ms
              {attempt.error ? ` · ${attempt.error}` : ''}
            </motion.text>
          </g>
        )
      })}
    </svg>
  )
}
