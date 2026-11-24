"""Recognize explicit Korean local-government names in publication headlines."""
import re
import unicodedata


_PROVINCES = {
    "서울특별시": "서울시", "부산광역시": "부산시", "대구광역시": "대구시",
    "인천광역시": "인천시", "광주광역시": "광주시", "대전광역시": "대전시",
    "울산광역시": "울산시", "세종특별자치시": "세종시", "경기도": "경기도",
    "강원특별자치도": "강원도", "충청북도": "충북도", "충청남도": "충남도",
    "전북특별자치도": "전북도", "전라남도": "전남도", "경상북도": "경북도",
    "경상남도": "경남도", "제주특별자치도": "제주도", "전라북도": "전북도",
}
_CITIES = """
수원 성남 의정부 안양 부천 광명 평택 동두천 안산 고양 과천 구리 남양주 오산 시흥 군포 의왕
하남 용인 파주 이천 안성 김포 화성 광주 양주 포천 여주 춘천 원주 강릉 동해 태백 속초 삼척
청주 충주 제천 천안 공주 보령 아산 서산 논산 계룡 당진 전주 군산 익산 정읍 남원 김제
목포 여수 순천 나주 광양 포항 경주 김천 안동 구미 영주 영천 상주 문경 경산 창원 진주 통영
사천 김해 밀양 거제 양산
"""
_COUNTIES = """
기장 달성 군위 강화 옹진 울주 연천 가평 양평 홍천 횡성 영월 평창 정선 철원 화천 양구 인제
고성 양양 보은 옥천 영동 증평 진천 괴산 음성 단양 금산 부여 서천 청양 홍성 예산 태안
완주 진안 무주 장수 임실 순창 고창 부안 담양 곡성 구례 고흥 보성 화순 장흥 강진 해남 영암
무안 함평 영광 장성 완도 진도 신안 의성 청송 영양 영덕 청도 고령 성주 칠곡 예천 봉화
울진 울릉 의령 함안 창녕 남해 하동 산청 함양 거창 합천
"""
_DISTRICTS = """
종로 용산 성동 광진 동대문 중랑 성북 강북 도봉 노원 은평 서대문 마포 양천 강서 구로 금천
영등포 동작 관악 서초 강남 송파 강동 영도 부산진 동래 해운대 사하 금정 연제 수영
사상 수성 달서 미추홀 연수 남동 부평 계양 광산 유성 대덕
"""
_ALIASES = {**_PROVINCES, **{name: name for name in _PROVINCES.values()}}
for names, suffix in ((_CITIES, "시"), (_COUNTIES, "군"), (_DISTRICTS, "구")):
    _ALIASES.update({name + suffix: name + suffix for name in names.split()})
# A right-hand particle is allowed, but organization names and compounds
# such as 울산시공사, 울산시민 and 울산시청소년센터 must not match.
_NAMES = "|".join(sorted(_ALIASES, key=lambda name: (-len(name), name)))
_MENTION = re.compile(
    rf"(?<![가-힣\w])({_NAMES})(?:\s*(?:시청|군청|구청|도청|청))?"
    r"(?:에서는|에서도|으로|까지|부터|에서|에게|[은는이가을를의도에와과로만])?(?![가-힣\w])"
)


def headline_municipalities(headline):
    return {_ALIASES[name] for name in _MENTION.findall(unicodedata.normalize("NFKC", headline))}
