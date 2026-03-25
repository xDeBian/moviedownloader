# KadriTV Download Manager

ბრაუზერზე დაფუძნებული Download Manager — mykadri.tv-სთვის.

## მოთხოვნები

- Python 3.8+
- pip

## ინსტალაცია

```bash
# 1. საჭირო ბიბლიოთეკების დაყენება
pip install -r requirements.txt

# 2. სერვერის გაშვება
python app.py
```

## გახსნა

ბრაუზერში: **http://localhost:5000**

---

## გამოყენება

### ფილმი
1. mykadri.tv-ზე გახსენი ფილმის გვერდი
2. კოპირება URL
3. ჩასვი ველში და დააჭირე **ანალიზი**
4. აარჩიე ხარისხი (1080p / 720p და ა.შ.)
5. დააჭირე **გადმოწერა**

### სერიალი
1. სერიალის მთავარ გვერდზე URL ჩასვი
2. ანალიზის შემდეგ გამოჩნდება ეპიზოდების სია
3. ყოველ ეპიზოდს ⬇️ ღილაკი აქვს
4. ასევე შეგიძლია ცალკეული ეპიზოდის URL-ით გადმოწერო

### პაუზა / გაგრძელება
- მიმდინარე გადმოწერაზე **⏸️ პაუზა** — ჩერდება
- **▶️ გაგრძელება** — თავიდან იწყება (yt-dlp --continue)

---

## გადმოწერილი ფაილები

ფაილები ინახება: `~/Downloads/KadriMovies/`

---

## ტექნიკური დეტალები

- **Backend**: Flask (Python)
- **Video extraction**: yt-dlp + BeautifulSoup
- **Player type**: JWPlayer (HLS/MP4)
- **Supported formats**: m3u8 (HLS), mp4, webm

## შენიშვნა JWPlayer-ზე

mykadri.tv JWPlayer-ს იყენებს. yt-dlp ავტომატურად ახდენს:
- HLS stream-ის პარსინგს
- საუკეთესო ხარისხის შერჩევას
- ffmpeg-ით merge-ს (video+audio)

თუ ffmpeg არ გაქვს:
```bash
# Windows: https://ffmpeg.org/download.html
# Mac:
brew install ffmpeg
# Linux:
sudo apt install ffmpeg
```
