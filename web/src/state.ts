/** 経路の状態。前処理の `STATE_CODE` と同じ（★map.ts・hazards.ts が共有。地図ライブラリに依存させない） */
export const STATE = { TOPO: 0, UNDERPASS: 1, KNOWN: 2 } as const;
