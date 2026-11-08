from pathlib import Path
import json
from subtitle_project import load_caption_project, save_caption_project, realign_caption_project_texts
from clip import create_clip

RUN_DIR = Path(r"C:\Storage\30_Software\90_Self_Made\Apps\clip_automation\data\runs\20260807_081656")

USER_IDEAL_TEXTS = [
    "俺もうさ",
    "全然100超えてるかと思ってさ",
    "おい参加力送ってくんじゃねーぞ",
    "ガキで",
    "精霊さもう200種類くらい",
    "いるかなと思ったらさ",
    "まだ100いないんだよ",
    "多分",
    "100いないから",
    "だるいじゃん集めんの",
    "えじゃあ全部集めんの",
    "みたいな",
    "今いるやつ",
    "全部集めないとダメなんみたいな",
    "それが嫌になっちゃって",
    "無理",
    "精霊集めは断念",
    "リアエルさん",
    "スーパーシャット5000円",
    "ありがとうございます",
    "参加型してほしい",
    "参加型",
    "参加型すんなら",
    "皆様から参加料の精霊",
    "もらえますけどね",
    "当然",
    "当然もらえますけどね",
    "なんで○○くんのこと",
    "フォローしたんですか",
    "フォロバしたんですか",
    "別に私が誰をフォロバしたっていいだろ",
    "なんでお前らに",
    "文句言わなきゃいけねえんだよ",
    "おいガキ",
    "いや",
    "私がとあるリスナーを",
    "Xでフォローバックしたら",
    "ふざけるなー",
    "なんでそいつだけフォロバするんだ",
    "みたいなね",
    "怒ってるガキが",
    "ワンサカワンサカ湧いてて",
    "うるせーなって思いましたね",
    "うん",
    "誰フォローしたっていいんですよ",
    "私が私は好きに",
    "フォローさせてもらいますよ",
    "うん",
    "えちえちな女の子だよ",
    "コスプレイヤーとか",
    "グラドル男フォローバックするわけねえだろ",
    "あでも",
    "今回フォローしたのは",
    "モッスーっていうガキだね",
    "モッスーっていうガキをフォロバしたら",
    "皆さんがなんでモッスーくんを",
    "フォローするんだってね怒って",
    "発狂して",
    "病む者まで現れて",
    "お前らいい加減しろよ",
    "お前らそんな細かいことに",
    "なんだ",
    "気にするくらいだったら",
    "夏休みの宿題でも",
    "やってくださいよ",
    "朝は8時に起きて",
    "午前中は夏休みの宿題",
    "そうだその前に",
    "ラジオ体操に行ってください",
    "皆さん朝6時に起きて",
    "ラジオ体操に行って",
    "その後NHKを見て",
    "夏休みの宿題やって",
    "午後から友達と遊びに行って",
    "お前らの仕事をして",
    "結構クールです",
    "お前らの仕事して",
    "ここです",
    "その日の話",
    "いや",
    "面白い映画から"
]

def test_rebuild_and_render():
    print("=== Re-aligning Subtitles with User Golden Ideal Model ===")
    clean_path = RUN_DIR / "clean_transcript.json"
    project_path = RUN_DIR / "caption_project.json"

    with open(clean_path, "r", encoding="utf-8") as f:
        raw_segments = json.load(f)

    project = load_caption_project(project_path)
    
    # ユーザーの手本全80行をタイムスタンプ全自動再同期
    realigned_project = realign_caption_project_texts(project, USER_IDEAL_TEXTS, raw_segments)
    save_caption_project(realigned_project, project_path)
    print(f"\nUpdated caption_project.json with {len(realigned_project['captions'])} ideal captions!")

    print("--- First 15 Subtitles Preview ---")
    for i, sub in enumerate(realigned_project["captions"][:15], 1):
        print(f"{i:02d}. [{sub['start']:.2f}s - {sub['end']:.2f}s] {sub['text']}")

    # 最高品質レンダリング
    print("\n=== Re-rendering Final Video with User Golden Ideal Model ===")
    final_output = RUN_DIR / "final_jumpcut_video.mp4"
    create_clip(
        project["source_path"],
        str(RUN_DIR / "edl.json"),
        str(RUN_DIR / "captions.ass"),
        str(final_output),
        caption_project_path=str(project_path),
        output_width=1920,
        output_height=1080,
        output_fps=60,
        encoder="auto"
    )
    print("\nVideo Rendering Completed Successfully!")
    print("Final Output Video:", final_output)

if __name__ == "__main__":
    test_rebuild_and_render()
