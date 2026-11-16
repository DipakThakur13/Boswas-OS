"""The places the world clock knows (standard library only).

Two sources: a table of large and well-known cities with their zone of
the time-zone database, and the database itself, whose zone names end in a
city (``America/Argentina/Buenos_Aires``). ``search`` looks in both; a city
whose zone this system's database lacks is left out.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from ..reminders import timeutil

# city|country|zone
_TABLE = """
Abidjan|Côte d'Ivoire|Africa/Abidjan
Abu Dhabi|United Arab Emirates|Asia/Dubai
Abuja|Nigeria|Africa/Lagos
Accra|Ghana|Africa/Accra
Addis Ababa|Ethiopia|Africa/Addis_Ababa
Adelaide|Australia|Australia/Adelaide
Ahmedabad|India|Asia/Kolkata
Alexandria|Egypt|Africa/Cairo
Algiers|Algeria|Africa/Algiers
Almaty|Kazakhstan|Asia/Almaty
Amman|Jordan|Asia/Amman
Amsterdam|Netherlands|Europe/Amsterdam
Anchorage|United States|America/Anchorage
Ankara|Türkiye|Europe/Istanbul
Antananarivo|Madagascar|Indian/Antananarivo
Antwerp|Belgium|Europe/Brussels
Asunción|Paraguay|America/Asuncion
Athens|Greece|Europe/Athens
Atlanta|United States|America/New_York
Auckland|New Zealand|Pacific/Auckland
Austin|United States|America/Chicago
Baghdad|Iraq|Asia/Baghdad
Baku|Azerbaijan|Asia/Baku
Bamako|Mali|Africa/Bamako
Bandung|Indonesia|Asia/Jakarta
Bangkok|Thailand|Asia/Bangkok
Barcelona|Spain|Europe/Madrid
Basel|Switzerland|Europe/Zurich
Beijing|China|Asia/Shanghai
Beirut|Lebanon|Asia/Beirut
Belfast|United Kingdom|Europe/London
Belgrade|Serbia|Europe/Belgrade
Belo Horizonte|Brazil|America/Sao_Paulo
Bengaluru|India|Asia/Kolkata
Bergen|Norway|Europe/Oslo
Berlin|Germany|Europe/Berlin
Bern|Switzerland|Europe/Zurich
Birmingham|United Kingdom|Europe/London
Bishkek|Kyrgyzstan|Asia/Bishkek
Bogotá|Colombia|America/Bogota
Bologna|Italy|Europe/Rome
Bordeaux|France|Europe/Paris
Boston|United States|America/New_York
Brasília|Brazil|America/Sao_Paulo
Bratislava|Slovakia|Europe/Bratislava
Brazzaville|Republic of the Congo|Africa/Brazzaville
Bremen|Germany|Europe/Berlin
Brisbane|Australia|Australia/Brisbane
Bristol|United Kingdom|Europe/London
Brussels|Belgium|Europe/Brussels
Bucharest|Romania|Europe/Bucharest
Budapest|Hungary|Europe/Budapest
Buenos Aires|Argentina|America/Argentina/Buenos_Aires
Busan|South Korea|Asia/Seoul
Cairo|Egypt|Africa/Cairo
Calgary|Canada|America/Edmonton
Cali|Colombia|America/Bogota
Canberra|Australia|Australia/Sydney
Cancún|Mexico|America/Cancun
Cape Town|South Africa|Africa/Johannesburg
Caracas|Venezuela|America/Caracas
Cardiff|United Kingdom|Europe/London
Casablanca|Morocco|Africa/Casablanca
Chengdu|China|Asia/Shanghai
Chennai|India|Asia/Kolkata
Chicago|United States|America/Chicago
Chișinău|Moldova|Europe/Chisinau
Chittagong|Bangladesh|Asia/Dhaka
Chongqing|China|Asia/Shanghai
Christchurch|New Zealand|Pacific/Auckland
Cologne|Germany|Europe/Berlin
Colombo|Sri Lanka|Asia/Colombo
Copenhagen|Denmark|Europe/Copenhagen
Córdoba|Argentina|America/Argentina/Cordoba
Curitiba|Brazil|America/Sao_Paulo
Dakar|Senegal|Africa/Dakar
Dallas|United States|America/Chicago
Damascus|Syria|Asia/Damascus
Dar es Salaam|Tanzania|Africa/Dar_es_Salaam
Darwin|Australia|Australia/Darwin
Delhi|India|Asia/Kolkata
Denver|United States|America/Denver
Detroit|United States|America/Detroit
Dhaka|Bangladesh|Asia/Dhaka
Doha|Qatar|Asia/Qatar
Dortmund|Germany|Europe/Berlin
Dresden|Germany|Europe/Berlin
Dubai|United Arab Emirates|Asia/Dubai
Dublin|Ireland|Europe/Dublin
Durban|South Africa|Africa/Johannesburg
Dushanbe|Tajikistan|Asia/Dushanbe
Düsseldorf|Germany|Europe/Berlin
Edinburgh|United Kingdom|Europe/London
Edmonton|Canada|America/Edmonton
El Paso|United States|America/Denver
Essen|Germany|Europe/Berlin
Faisalabad|Pakistan|Asia/Karachi
Florence|Italy|Europe/Rome
Fortaleza|Brazil|America/Fortaleza
Frankfurt|Germany|Europe/Berlin
Fukuoka|Japan|Asia/Tokyo
Gdańsk|Poland|Europe/Warsaw
Geneva|Switzerland|Europe/Zurich
Genoa|Italy|Europe/Rome
Glasgow|United Kingdom|Europe/London
Gothenburg|Sweden|Europe/Stockholm
Graz|Austria|Europe/Vienna
Guadalajara|Mexico|America/Mexico_City
Guangzhou|China|Asia/Shanghai
Guatemala City|Guatemala|America/Guatemala
Guayaquil|Ecuador|America/Guayaquil
Halifax|Canada|America/Halifax
Hamburg|Germany|Europe/Berlin
Hangzhou|China|Asia/Shanghai
Hanoi|Vietnam|Asia/Bangkok
Hanover|Germany|Europe/Berlin
Harare|Zimbabwe|Africa/Harare
Havana|Cuba|America/Havana
Helsinki|Finland|Europe/Helsinki
Ho Chi Minh City|Vietnam|Asia/Ho_Chi_Minh
Hobart|Australia|Australia/Hobart
Hong Kong|China|Asia/Hong_Kong
Honolulu|United States|Pacific/Honolulu
Houston|United States|America/Chicago
Hyderabad|India|Asia/Kolkata
Ibadan|Nigeria|Africa/Lagos
Indianapolis|United States|America/Indiana/Indianapolis
Innsbruck|Austria|Europe/Vienna
Islamabad|Pakistan|Asia/Karachi
Istanbul|Türkiye|Europe/Istanbul
İzmir|Türkiye|Europe/Istanbul
Jaipur|India|Asia/Kolkata
Jakarta|Indonesia|Asia/Jakarta
Jeddah|Saudi Arabia|Asia/Riyadh
Jerusalem|Israel|Asia/Jerusalem
Johannesburg|South Africa|Africa/Johannesburg
Kabul|Afghanistan|Asia/Kabul
Kampala|Uganda|Africa/Kampala
Kano|Nigeria|Africa/Lagos
Kansas City|United States|America/Chicago
Karachi|Pakistan|Asia/Karachi
Kathmandu|Nepal|Asia/Kathmandu
Khartoum|Sudan|Africa/Khartoum
Kigali|Rwanda|Africa/Kigali
Kingston|Jamaica|America/Jamaica
Kinshasa|DR Congo|Africa/Kinshasa
Kolkata|India|Asia/Kolkata
Kraków|Poland|Europe/Warsaw
Kuala Lumpur|Malaysia|Asia/Kuala_Lumpur
Kuwait City|Kuwait|Asia/Kuwait
Kyiv|Ukraine|Europe/Kyiv
Kyoto|Japan|Asia/Tokyo
La Paz|Bolivia|America/La_Paz
Lagos|Nigeria|Africa/Lagos
Lahore|Pakistan|Asia/Karachi
Las Vegas|United States|America/Los_Angeles
Lausanne|Switzerland|Europe/Zurich
Leeds|United Kingdom|Europe/London
Leipzig|Germany|Europe/Berlin
Lima|Peru|America/Lima
Linz|Austria|Europe/Vienna
Lisbon|Portugal|Europe/Lisbon
Liverpool|United Kingdom|Europe/London
Ljubljana|Slovenia|Europe/Ljubljana
London|United Kingdom|Europe/London
Los Angeles|United States|America/Los_Angeles
Luanda|Angola|Africa/Luanda
Lusaka|Zambia|Africa/Lusaka
Luxembourg|Luxembourg|Europe/Luxembourg
Lyon|France|Europe/Paris
Madrid|Spain|Europe/Madrid
Málaga|Spain|Europe/Madrid
Malmö|Sweden|Europe/Stockholm
Managua|Nicaragua|America/Managua
Manaus|Brazil|America/Manaus
Manchester|United Kingdom|Europe/London
Manila|Philippines|Asia/Manila
Maputo|Mozambique|Africa/Maputo
Marrakesh|Morocco|Africa/Casablanca
Marseille|France|Europe/Paris
Mecca|Saudi Arabia|Asia/Riyadh
Medellín|Colombia|America/Bogota
Melbourne|Australia|Australia/Melbourne
Mexico City|Mexico|America/Mexico_City
Miami|United States|America/New_York
Milan|Italy|Europe/Rome
Minneapolis|United States|America/Chicago
Minsk|Belarus|Europe/Minsk
Mogadishu|Somalia|Africa/Mogadishu
Monterrey|Mexico|America/Monterrey
Montevideo|Uruguay|America/Montevideo
Montréal|Canada|America/Toronto
Moscow|Russia|Europe/Moscow
Mumbai|India|Asia/Kolkata
Munich|Germany|Europe/Berlin
Muscat|Oman|Asia/Muscat
Nagoya|Japan|Asia/Tokyo
Nairobi|Kenya|Africa/Nairobi
Nanjing|China|Asia/Shanghai
Naples|Italy|Europe/Rome
Nashville|United States|America/Chicago
New Orleans|United States|America/Chicago
New York|United States|America/New_York
Nice|France|Europe/Paris
Novosibirsk|Russia|Asia/Novosibirsk
Nuremberg|Germany|Europe/Berlin
Nuuk|Greenland|America/Nuuk
Odesa|Ukraine|Europe/Kyiv
Orlando|United States|America/New_York
Osaka|Japan|Asia/Tokyo
Oslo|Norway|Europe/Oslo
Ottawa|Canada|America/Toronto
Ouagadougou|Burkina Faso|Africa/Ouagadougou
Palermo|Italy|Europe/Rome
Panama City|Panama|America/Panama
Paris|France|Europe/Paris
Perth|Australia|Australia/Perth
Philadelphia|United States|America/New_York
Phnom Penh|Cambodia|Asia/Phnom_Penh
Phoenix|United States|America/Phoenix
Pittsburgh|United States|America/New_York
Port-au-Prince|Haiti|America/Port-au-Prince
Portland|United States|America/Los_Angeles
Porto|Portugal|Europe/Lisbon
Porto Alegre|Brazil|America/Sao_Paulo
Prague|Czechia|Europe/Prague
Pretoria|South Africa|Africa/Johannesburg
Pune|India|Asia/Kolkata
Pyongyang|North Korea|Asia/Pyongyang
Québec|Canada|America/Toronto
Quito|Ecuador|America/Guayaquil
Rabat|Morocco|Africa/Casablanca
Recife|Brazil|America/Recife
Regina|Canada|America/Regina
Reykjavík|Iceland|Atlantic/Reykjavik
Riga|Latvia|Europe/Riga
Rio de Janeiro|Brazil|America/Sao_Paulo
Riyadh|Saudi Arabia|Asia/Riyadh
Rome|Italy|Europe/Rome
Rotterdam|Netherlands|Europe/Amsterdam
Saint Petersburg|Russia|Europe/Moscow
Salt Lake City|United States|America/Denver
Salvador|Brazil|America/Bahia
Salzburg|Austria|Europe/Vienna
San Antonio|United States|America/Chicago
San Diego|United States|America/Los_Angeles
San Francisco|United States|America/Los_Angeles
San José|Costa Rica|America/Costa_Rica
San Juan|Puerto Rico|America/Puerto_Rico
San Salvador|El Salvador|America/El_Salvador
Santiago|Chile|America/Santiago
Santo Domingo|Dominican Republic|America/Santo_Domingo
São Paulo|Brazil|America/Sao_Paulo
Sapporo|Japan|Asia/Tokyo
Sarajevo|Bosnia and Herzegovina|Europe/Sarajevo
Seattle|United States|America/Los_Angeles
Seoul|South Korea|Asia/Seoul
Seville|Spain|Europe/Madrid
Shanghai|China|Asia/Shanghai
Shenzhen|China|Asia/Shanghai
Singapore|Singapore|Asia/Singapore
Skopje|North Macedonia|Europe/Skopje
Sofia|Bulgaria|Europe/Sofia
St. John's|Canada|America/St_Johns
Stockholm|Sweden|Europe/Stockholm
Strasbourg|France|Europe/Paris
Stuttgart|Germany|Europe/Berlin
Surabaya|Indonesia|Asia/Jakarta
Sydney|Australia|Australia/Sydney
Taipei|Taiwan|Asia/Taipei
Tallinn|Estonia|Europe/Tallinn
Tashkent|Uzbekistan|Asia/Tashkent
Tbilisi|Georgia|Asia/Tbilisi
Tegucigalpa|Honduras|America/Tegucigalpa
Tehran|Iran|Asia/Tehran
Tel Aviv|Israel|Asia/Jerusalem
The Hague|Netherlands|Europe/Amsterdam
Thessaloniki|Greece|Europe/Athens
Tianjin|China|Asia/Shanghai
Tijuana|Mexico|America/Tijuana
Tirana|Albania|Europe/Tirane
Tokyo|Japan|Asia/Tokyo
Toronto|Canada|America/Toronto
Toulouse|France|Europe/Paris
Tripoli|Libya|Africa/Tripoli
Tunis|Tunisia|Africa/Tunis
Turin|Italy|Europe/Rome
Ulaanbaatar|Mongolia|Asia/Ulaanbaatar
Valencia|Spain|Europe/Madrid
Valletta|Malta|Europe/Malta
Vancouver|Canada|America/Vancouver
Venice|Italy|Europe/Rome
Vienna|Austria|Europe/Vienna
Vientiane|Laos|Asia/Vientiane
Vilnius|Lithuania|Europe/Vilnius
Vladivostok|Russia|Asia/Vladivostok
Warsaw|Poland|Europe/Warsaw
Washington, D.C.|United States|America/New_York
Wellington|New Zealand|Pacific/Auckland
Windhoek|Namibia|Africa/Windhoek
Winnipeg|Canada|America/Winnipeg
Wrocław|Poland|Europe/Warsaw
Wuhan|China|Asia/Shanghai
Xi'an|China|Asia/Shanghai
Yangon|Myanmar|Asia/Yangon
Yaoundé|Cameroon|Africa/Douala
Yekaterinburg|Russia|Asia/Yekaterinburg
Yerevan|Armenia|Asia/Yerevan
Yokohama|Japan|Asia/Tokyo
Zagreb|Croatia|Europe/Zagreb
Zürich|Switzerland|Europe/Zurich
"""
# Zones of the database that were renamed: the table uses the new name, older systems have the old one.
_OLD_NAMES = {"Europe/Kyiv": "Europe/Kiev", "America/Nuuk": "America/Godthab", "Asia/Yangon": "Asia/Rangoon",
              "Asia/Kathmandu": "Asia/Katmandu", "Asia/Kolkata": "Asia/Calcutta",
              "Asia/Ho_Chi_Minh": "Asia/Saigon"}
_SKIPPED_AREAS = ("Etc", "SystemV", "US", "Brazil", "Canada", "Chile", "Mexico", "posix", "right")


@dataclass(frozen=True)
class Place:
    name: str              # the city
    country: str           # empty for a place that comes from the database alone
    zone: str

    @property
    def label(self) -> str:
        return f"{self.name}, {self.country}" if self.country else self.name


def fold(text: str) -> str:
    """Lower case without accents and punctuation, for comparing what people type."""
    decomposed = unicodedata.normalize("NFKD", text.casefold().replace("ı", "i").replace("ł", "l")
                                       .replace("ø", "o").replace("đ", "d"))
    return "".join(character for character in decomposed
                   if not unicodedata.combining(character) and (character.isalnum() or character == " ")).strip()


@lru_cache(maxsize=1)
def places() -> tuple[Place, ...]:
    """Every place the world clock offers on this system, by name."""
    found: list[Place] = []
    listed_zones = set()
    names = set()
    for line in _TABLE.strip().splitlines():
        name, country, zone = line.split("|")
        if timeutil.zone(zone) is None:
            zone = _OLD_NAMES.get(zone, "")
            if not zone or timeutil.zone(zone) is None:
                continue
        found.append(Place(name, country, zone))
        listed_zones.add(zone)
        names.add(fold(name))
    for zone in timeutil.zone_names():
        if "/" not in zone or zone.split("/")[0] in _SKIPPED_AREAS:
            continue
        name = zone.rsplit("/", 1)[1].replace("_", " ")
        if fold(name) in names:
            continue
        names.add(fold(name))
        found.append(Place(name, "", zone))
    found.sort(key=lambda place: fold(place.name))
    return tuple(found)


def search(query: str, limit: int = 60) -> list[Place]:
    """Places whose city, country or zone matches ``query``: names that begin with it first."""
    wanted = fold(query)
    if not wanted:
        return list(places()[:limit])
    begins, words, contains = [], [], []
    for place in places():
        name = fold(place.name)
        if name.startswith(wanted):
            begins.append(place)
        elif any(word.startswith(wanted) for word in name.split()):
            words.append(place)
        elif wanted in name or wanted in fold(place.country) or wanted in fold(place.zone.replace("/", " ").replace(
                "_", " ")):
            contains.append(place)
    return (begins + words + contains)[:limit]


def name_for_zone(zone: str) -> str:
    """What to call a zone when no city was chosen: the city of its name."""
    return zone.rsplit("/", 1)[-1].replace("_", " ")
