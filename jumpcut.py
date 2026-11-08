import json
import re
import os

# 基本的なフィラー（ケバ）を検出する正規表現
FILLER_REGEX = re.compile(r'^(えー|あー|あの|あのー|えっと|なんか|えーと|んー|うー|うーん|ま|まあ)[っー]*$')
MERGE_THRESHOLD = 0.3  # この秒数以下の無音（単語間ギャップ）はカットせず結合する

def is_filler(text: str) -> bool:
    # 句読点や空白を除去して判定
    text = re.sub(r'[、。！？\s]', '', text)
    if not text:
        return True # 記号のみの場合はカット対象とする
    return bool(FILLER_REGEX.match(text))

def generate_jumpcut_data(transcript_json: str, output_edl: str, output_clean_transcript: str):
    """
    文字起こしデータからフィラーと無音を除外したEdit Decision List (EDL) と、
    タイムスタンプをジャンプカット後の時間に再計算したクリーンな文字起こしを生成する。
    """
    with open(transcript_json, 'r', encoding='utf-8') as f:
        segments = json.load(f)
    
    keep_words = []
    
    # 1. フィラーを除外して残す単語をリストアップ
    for seg in segments:
        for word_info in seg.get('words', []):
            if not is_filler(word_info['word']):
                keep_words.append(word_info)
    
    if not keep_words:
        raise ValueError(
            "有効な発話語が見つかりませんでした。発話のある区間を選んでください。"
        )
        
    # 2. 近接する単語を一つのインターバル（残す区間）にまとめる
    intervals = []
    current_interval = {
        'orig_start': keep_words[0]['start'],
        'orig_end': keep_words[0]['end'],
        'words': [keep_words[0]]
    }
    
    for word in keep_words[1:]:
        gap = word['start'] - current_interval['orig_end']
        if gap <= MERGE_THRESHOLD:
            # ギャップが小さければ区間を延長して結合
            current_interval['orig_end'] = max(current_interval['orig_end'], word['end'])
            current_interval['words'].append(word)
        else:
            # ギャップが大きい場合は新しい区間として分離（ここで無音がカットされる）
            intervals.append(current_interval)
            current_interval = {
                'orig_start': word['start'],
                'orig_end': word['end'],
                'words': [word]
            }
    intervals.append(current_interval)
    
    # 3. EDLの生成
    edl = [{"start": iv['orig_start'], "end": iv['orig_end']} for iv in intervals]
    
    # 4. 新しいタイムスタンプを持つ文字起こしデータの生成
    clean_transcript = []
    current_new_time = 0.0
    
    for iv in intervals:
        duration = iv['orig_end'] - iv['orig_start']
        
        adjusted_words = []
        for w in iv['words']:
            # 元の区間開始位置からのオフセットを計算
            offset = w['start'] - iv['orig_start']
            word_duration = w['end'] - w['start']
            
            new_word_start = current_new_time + offset
            new_word_end = new_word_start + word_duration
            
            adjusted_words.append({
                "word": w['word'],
                "start": round(new_word_start, 3),
                "end": round(new_word_end, 3)
            })
            
        # 再構成されたテキスト
        text = "".join(w['word'] for w in adjusted_words)
        
        clean_transcript.append({
            "start": round(current_new_time, 3),
            "end": round(current_new_time + duration, 3),
            "text": text,
            "words": adjusted_words
        })
        
        current_new_time += duration
        
    os.makedirs(os.path.dirname(output_edl) or '.', exist_ok=True)
    os.makedirs(os.path.dirname(output_clean_transcript) or '.', exist_ok=True)

    with open(output_edl, 'w', encoding='utf-8') as f:
        json.dump(edl, f, ensure_ascii=False, indent=2)
        
    with open(output_clean_transcript, 'w', encoding='utf-8') as f:
        json.dump(clean_transcript, f, ensure_ascii=False, indent=2)
        
    print(f"EDL生成完了: {len(edl)} 区間を残します（無音・フィラーを除外）。")
    print(f" - EDL保存先: {output_edl}")
    print(f" - クリーンな文字起こし保存先: {output_clean_transcript}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 3:
        generate_jumpcut_data(sys.argv[1], sys.argv[2], sys.argv[3])
    else:
        print("Usage: python jumpcut.py <input_transcript.json> <output_edl.json> <output_clean_transcript.json>")
