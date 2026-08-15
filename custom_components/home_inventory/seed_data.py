"""Seed DB with common Israeli grocery products.

Barcodes starting with 729 are Israeli. We don't ship verified barcodes
for every product (those change per SKU/size). Instead, we seed the
product name table so free-text search finds common items instantly,
and real barcodes get added to the cache as users scan them.

Where a well-known, stable barcode is public knowledge it is included;
otherwise the barcode is left NULL and the product is searchable by name.
"""
from __future__ import annotations

import logging
from .storage import Database

_LOGGER = logging.getLogger(__name__)

# (name_en, name_he, brand, category, barcode_or_None)
SEED_PRODUCTS: list[tuple[str, str, str | None, str, str | None]] = [
    # --- Dairy (תנובה, טרה, יטבתה, שטראוס) ---
    ("Cottage Cheese 5%", "קוטג' 5%", "תנובה", "dairy", None),
    ("Cottage Cheese 3%", "קוטג' 3%", "תנובה", "dairy", None),
    ("White Cheese 5%", "גבינה לבנה 5%", "תנובה", "dairy", None),
    ("White Cheese 9%", "גבינה לבנה 9%", "תנובה", "dairy", None),
    ("Milk 3% 1L", "חלב 3% ליטר", "תנובה", "dairy", None),
    ("Milk 1% 1L", "חלב 1% ליטר", "תנובה", "dairy", None),
    ("Milk 3% bag", "חלב 3% שקית", "תנובה", "dairy", None),
    ("Chocolate Milk (Shoko) bag", "שוקו בשקית", "תנובה", "dairy", None),
    ("Butter 200g", "חמאה 200 גרם", "תנובה", "dairy", None),
    ("Yellow Cheese Emek", "גבינת עמק", "תנובה", "dairy", None),
    ("Yellow Cheese Gilboa", "גבינת גלבוע", "תנובה", "dairy", None),
    ("Yogurt plain", "יוגורט טבעי", "תנובה", "dairy", None),
    ("Yogurt fruit", "יוגורט פירות", "תנובה", "dairy", None),
    ("Leben", "לבן", "תנובה", "dairy", None),
    ("Sour cream 15%", "שמנת חמוצה 15%", "תנובה", "dairy", None),
    ("Sweet cream 38%", "שמנת מתוקה 38%", "תנובה", "dairy", None),
    ("Eshel", "אשל", "תנובה", "dairy", None),
    ("Gil yogurt", "גיל", "תנובה", "dairy", None),
    ("Milky dessert", "מילקי", "שטראוס", "dairy", None),
    ("Danone yogurt", "דנונה", "שטראוס", "dairy", None),
    ("Yoplait", "יופלה", "שטראוס", "dairy", None),
    ("Feta cheese", "גבינת פטה", "תנובה", "dairy", None),
    ("Bulgarian cheese", "גבינה בולגרית", "תנובה", "dairy", None),
    ("Tzfatit cheese", "גבינה צפתית", "תנובה", "dairy", None),
    ("Cream cheese", "גבינת שמנת", "תנובה", "dairy", None),
    ("Labane", "לבנה", "תנובה", "dairy", None),

    # --- Bakery ---
    ("Sliced bread white", "לחם פרוס לבן", "אנג'ל", "bakery", None),
    ("Sliced bread whole wheat", "לחם פרוס מלא", "אנג'ל", "bakery", None),
    ("Pita bread", "פיתות", "אנג'ל", "bakery", None),
    ("Lachmaniyot (buns)", "לחמניות", "אנג'ל", "bakery", None),
    ("Challah", "חלה", "אנג'ל", "bakery", None),
    ("Bagele", "בייגלה", "אסם", "bakery", None),

    # --- Produce ---
    ("Tomatoes", "עגבניות", None, "produce", None),
    ("Cucumbers", "מלפפונים", None, "produce", None),
    ("Onions", "בצל", None, "produce", None),
    ("Garlic", "שום", None, "produce", None),
    ("Potatoes", "תפוחי אדמה", None, "produce", None),
    ("Sweet potatoes", "בטטה", None, "produce", None),
    ("Carrots", "גזר", None, "produce", None),
    ("Peppers", "פלפלים", None, "produce", None),
    ("Lettuce", "חסה", None, "produce", None),
    ("Cabbage", "כרוב", None, "produce", None),
    ("Parsley", "פטרוזיליה", None, "produce", None),
    ("Cilantro", "כוסברה", None, "produce", None),
    ("Mint", "נענע", None, "produce", None),
    ("Lemons", "לימונים", None, "produce", None),
    ("Bananas", "בננות", None, "produce", None),
    ("Apples", "תפוחי עץ", None, "produce", None),
    ("Oranges", "תפוזים", None, "produce", None),
    ("Clementines", "קלמנטינות", None, "produce", None),
    ("Avocado", "אבוקדו", None, "produce", None),
    ("Eggplant", "חציל", None, "produce", None),
    ("Zucchini", "קישוא", None, "produce", None),
    ("Mushrooms", "פטריות", None, "produce", None),

    # --- Meat/Fish ---
    ("Chicken breast", "חזה עוף", None, "meat_fish", None),
    ("Chicken thighs", "שוקיים", None, "meat_fish", None),
    ("Ground beef", "בשר טחון", None, "meat_fish", None),
    ("Entrecote", "אנטריקוט", None, "meat_fish", None),
    ("Schnitzel", "שניצל", None, "meat_fish", None),
    ("Salmon fillet", "סלמון", None, "meat_fish", None),
    ("Denis (seabream)", "דניס", None, "meat_fish", None),
    ("Tuna can", "טונה", "סטארקיסט", "meat_fish", None),
    ("Tuna can (Willi Food)", "טונה וילי", "וילי פוד", "meat_fish", None),

    # --- Frozen ---
    ("Frozen peas", "אפונה קפואה", "סנפרוסט", "frozen", None),
    ("Frozen corn", "תירס קפוא", "סנפרוסט", "frozen", None),
    ("Frozen mixed vegetables", "ירקות קפואים", "סנפרוסט", "frozen", None),
    ("Frozen broccoli", "ברוקולי קפוא", "סנפרוסט", "frozen", None),
    ("Ice cream Magnum", "מגנום", "שטראוס", "frozen", None),
    ("Popsicles Kartiv", "קרטיב", "שטראוס", "frozen", None),
    ("Ice cream bucket", "גלידה קופסה", "שטראוס", "frozen", None),

    # --- Pantry: staples ---
    ("Rice 1kg", "אורז 1 ק\"ג", "סוגת", "pantry", None),
    ("Basmati rice", "אורז בסמטי", "סוגת", "pantry", None),
    ("Pasta spaghetti", "פסטה ספגטי", "אסם", "pantry", None),
    ("Pasta penne", "פסטה פנה", "אסם", "pantry", None),
    ("Couscous", "קוסקוס", "אסם", "pantry", None),
    ("Bulgur", "בורגול", "סוגת", "pantry", None),
    ("Flour 1kg", "קמח 1 ק\"ג", "סוגת", "pantry", None),
    ("Whole wheat flour", "קמח מלא", "סוגת", "pantry", None),
    ("Sugar 1kg", "סוכר 1 ק\"ג", "סוגת", "pantry", None),
    ("Brown sugar", "סוכר חום", "סוגת", "pantry", None),
    ("Salt", "מלח", "אסם", "pantry", None),
    ("Black pepper", "פלפל שחור", "אסם", "pantry", None),
    ("Paprika", "פפריקה", "אסם", "pantry", None),
    ("Cumin", "כמון", "אסם", "pantry", None),
    ("Turmeric", "כורכום", "אסם", "pantry", None),
    ("Zaatar", "זעתר", None, "pantry", None),
    ("Baking powder", "אבקת אפייה", "אסם", "pantry", None),
    ("Baking soda", "סודה לשתייה", None, "pantry", None),
    ("Yeast", "שמרים", "שמרית", "pantry", None),
    ("Vanilla sugar", "סוכר וניל", "אסם", "pantry", None),
    ("Cocoa powder", "אבקת קקאו", "עלית", "pantry", None),
    ("Corn starch", "קורנפלור", "אסם", "pantry", None),
    ("Olive oil", "שמן זית", None, "pantry", None),
    ("Canola oil", "שמן קנולה", "עין הבר", "pantry", None),
    ("Vinegar", "חומץ", "תלמה", "pantry", None),
    ("Soy sauce", "רוטב סויה", "אסם", "pantry", None),
    ("Ketchup", "קטשופ", "אסם", "pantry", None),
    ("Mayonnaise", "מיונז", "תלמה", "pantry", None),
    ("Mustard", "חרדל", "תלמה", "pantry", None),
    ("Tahina raw", "טחינה גולמית", "הר ברכה", "pantry", None),
    ("Tahina prepared", "טחינה מוכנה", "אחוה", "pantry", None),
    ("Hummus can", "חומוס קופסה", "צבר", "pantry", None),
    ("Tomato paste", "רסק עגבניות", "יכין", "pantry", None),
    ("Tomato sauce", "רוטב עגבניות", "יכין", "pantry", None),
    ("Pickles", "חמוצים", "בית השיטה", "pantry", None),
    ("Olives green", "זיתים ירוקים", "בית השיטה", "pantry", None),
    ("Olives black", "זיתים שחורים", "בית השיטה", "pantry", None),
    ("Chickpeas can", "גרגירי חומוס", "יכין", "pantry", None),
    ("Corn can", "תירס קופסה", "גרין פילד", "pantry", None),
    ("Beans can", "שעועית קופסה", "יכין", "pantry", None),
    ("Lentils", "עדשים", "סוגת", "pantry", None),
    ("Red lentils", "עדשים אדומות", "סוגת", "pantry", None),
    ("Honey", "דבש", "יד מרדכי", "pantry", None),
    ("Silan", "סילאן", "גת", "pantry", None),
    ("Jam strawberry", "ריבת תות", "פרי ניב", "pantry", None),
    ("Chocolate spread", "ממרח שוקולד", "הרטוג", "pantry", None),
    ("Peanut butter", "חמאת בוטנים", "קוסט", "pantry", None),
    ("Cornflakes", "קורנפלקס", "תלמה", "pantry", None),
    ("Branflakes", "בראן פלקס", "תלמה", "pantry", None),
    ("Cheerios", "צ'יריוס", "תלמה", "pantry", None),
    ("Oatmeal", "קוואקר", "תלמה", "pantry", None),
    ("Eggs L 12", "ביצים L תריסר", None, "pantry", None),
    ("Eggs XL 12", "ביצים XL תריסר", None, "pantry", None),

    # --- Beverages ---
    ("Mineral water 1.5L", "מים 1.5 ליטר", "מי עדן", "beverages", None),
    ("Mineral water 6-pack", "מים שישייה", "נביעות", "beverages", None),
    ("Soda water", "סודה", "סודה סטרים", "beverages", None),
    ("Coca Cola 1.5L", "קוקה קולה 1.5", "קוקה קולה", "beverages", None),
    ("Coca Cola Zero 1.5L", "קוקה קולה זירו", "קוקה קולה", "beverages", None),
    ("Sprite 1.5L", "ספרייט", "קוקה קולה", "beverages", None),
    ("Fanta 1.5L", "פאנטה", "קוקה קולה", "beverages", None),
    ("Prigat orange", "פריגת תפוזים", "פריגת", "beverages", None),
    ("Prigat apple", "פריגת תפוחים", "פריגת", "beverages", None),
    ("Tapuzina", "תפוזינה", "תפוזינה", "beverages", None),
    ("Coffee Elite", "קפה עלית", "עלית", "beverages", None),
    ("Turkish coffee", "קפה טורקי", "עלית", "beverages", None),
    ("Coffee Nescafe", "נסקפה", "נסטלה", "beverages", None),
    ("Tea Wissotzky", "תה ויסוצקי", "ויסוצקי", "beverages", None),
    ("Tea Nana", "תה נענע", "ויסוצקי", "beverages", None),
    ("Beer Goldstar 6-pack", "גולדסטאר שישייה", "טמפו", "beverages", None),
    ("Beer Maccabi 6-pack", "מכבי שישייה", "טמפו", "beverages", None),
    ("Wine dry red", "יין אדום יבש", None, "beverages", None),
    ("Wine semi dry white", "יין לבן חצי יבש", None, "beverages", None),

    # --- Snacks (אסם, עלית, שטראוס, בייגל בייגל) ---
    ("Bamba", "במבה", "אסם", "snacks", None),
    ("Bissli grill", "ביסלי גריל", "אסם", "snacks", None),
    ("Bissli falafel", "ביסלי פלאפל", "אסם", "snacks", None),
    ("Bissli smoke", "ביסלי סמוק", "אסם", "snacks", None),
    ("Bissli BBQ", "ביסלי בר־בי־קיו", "אסם", "snacks", None),
    ("Apropo", "אפרופו", "אסם", "snacks", None),
    ("Cheetos", "צ'יטוס", "עלית", "snacks", None),
    ("Doritos", "דוריטוס", "עלית", "snacks", None),
    ("Pretzels", "בייגלה", "בייגל בייגל", "snacks", None),
    ("Kinder Bueno", "קינדר בואנו", "פרררו", "snacks", None),
    ("Milk chocolate (Shachar)", "שוקולד חלב שחר", "עלית", "snacks", None),
    ("Pesek Zman", "פסק זמן", "עלית", "snacks", None),
    ("Kif Kef", "קיף קף", "עלית", "snacks", None),
    ("Parah chocolate", "שוקולד פרה", "עלית", "snacks", None),
    ("Klik", "קליק", "עלית", "snacks", None),
    ("Mekupelet", "מקופלת", "עלית", "snacks", None),
    ("Tortit", "טורטית", "עלית", "snacks", None),
    ("Krembo", "קרמבו", "שטראוס", "snacks", None),
    ("Egozi", "אגוזי", "עלית", "snacks", None),
    ("Sunflower seeds", "גרעינים שחורים", "תפוגן", "snacks", None),
    ("Pumpkin seeds", "גרעיני דלעת", "תפוגן", "snacks", None),
    ("Peanuts Americani", "בוטנים אמריקני", "עלית", "snacks", None),
    ("Corn snack", "אבטיחים (חטיף)", "אסם", "snacks", None),
    ("Rice cakes", "פריכיות אורז", "תלמה", "snacks", None),
    ("Crackers cream", "קרקרים", "אסם", "snacks", None),
    ("Petit Beurre", "פתי בר", "אסם", "snacks", None),

    # --- Cleaning ---
    ("Dish soap Sano Tapuach", "סבון כלים סנו", "סנו", "cleaning", None),
    ("Dish soap Palmolive", "סבון כלים פלמוליב", "פלמוליב", "cleaning", None),
    ("Floor cleaner Sano", "סבון רצפה סנו", "סנו", "cleaning", None),
    ("Bleach", "אקונומיקה", "סנו", "cleaning", None),
    ("Laundry detergent Bio", "אבקת כביסה ביו", "ביו", "cleaning", None),
    ("Laundry softener Badin", "מרכך כביסה בדין", "בדין", "cleaning", None),
    ("Toilet paper 48", "נייר טואלט 48", "ליל", "cleaning", None),
    ("Kitchen towels", "מגבות מטבח", "ליל", "cleaning", None),
    ("Garbage bags", "שקיות אשפה", "סנו", "cleaning", None),
    ("Tissues", "טישו", "ליל", "cleaning", None),
    ("Wipes", "מגבונים", "האגיס", "cleaning", None),
    ("Aluminum foil", "נייר כסף", "פרספקט", "cleaning", None),
    ("Cling film", "ניילון נצמד", "פרספקט", "cleaning", None),
    ("Baking paper", "נייר אפייה", "פרספקט", "cleaning", None),
    ("Dishwasher tablets", "טבליות מדיח", "סומט", "cleaning", None),
    ("Sponges", "ספוגים", "סקוצ' ברייט", "cleaning", None),

    # --- Personal care ---
    ("Shampoo", "שמפו", "לוריאל", "personal_care", None),
    ("Conditioner", "מרכך שיער", "לוריאל", "personal_care", None),
    ("Shower gel", "סבון רחצה", "דאב", "personal_care", None),
    ("Hand soap", "סבון ידיים", "דאב", "personal_care", None),
    ("Toothpaste Colgate", "משחת שיניים קולגייט", "קולגייט", "personal_care", None),
    ("Toothpaste Sensodyne", "משחת שיניים סנסודיין", "סנסודיין", "personal_care", None),
    ("Deodorant", "דאודורנט", "דאב", "personal_care", None),
    ("Razors", "סכיני גילוח", "ג'ילט", "personal_care", None),
    ("Diapers S", "חיתולים S", "האגיס", "personal_care", None),
    ("Diapers M", "חיתולים M", "האגיס", "personal_care", None),
    ("Diapers L", "חיתולים L", "האגיס", "personal_care", None),
    ("Baby wipes", "מגבונים לתינוק", "האגיס", "personal_care", None),
]


async def seed_if_empty(db: Database) -> None:
    """Seed the DB once if there are no products yet."""
    existing = await db.search_products("", limit=1)
    if existing:
        return

    _LOGGER.info("Seeding Home Inventory DB with %d products", len(SEED_PRODUCTS))
    for name_en, name_he, brand, category, barcode in SEED_PRODUCTS:
        await db.upsert_product(
            barcode=barcode,
            name=name_en,
            name_he=name_he,
            brand=brand,
            category=category,
            source="seed",
        )
