"""Spending taxonomy. Gemini must pick from these, so the charts stay clean."""

TAXONOMY: dict[str, dict] = {
    "Groceries": {"color": "#2f9e44", "subs": [
        "Produce", "Meat & Seafood", "Dairy & Eggs", "Bakery", "Pantry & Dry Goods",
        "Frozen", "Snacks & Sweets", "Beverages", "Prepared Foods", "Spices & Ethnic Foods"]},
    "Dining Out": {"color": "#e8590c", "subs": [
        "Restaurants", "Fast Food", "Coffee & Tea", "Bars", "Food Delivery", "Desserts"]},
    "Alcohol": {"color": "#a61e4d", "subs": ["Beer", "Wine", "Spirits"]},
    "Household": {"color": "#1971c2", "subs": [
        "Cleaning Supplies", "Paper Goods", "Kitchenware", "Home Improvement", "Furniture & Decor",
        "Laundry"]},
    "Personal Care": {"color": "#c2255c", "subs": ["Toiletries", "Beauty & Cosmetics", "Haircut & Salon"]},
    "Health": {"color": "#0c8599", "subs": ["Pharmacy", "Vitamins & Supplements", "Doctor & Dental", "Fitness"]},
    "Transport": {"color": "#5f3dc4", "subs": [
        "Gas & Fuel", "Parking & Tolls", "Rideshare & Taxi", "Public Transit", "Car Maintenance", "EV Charging"]},
    "Shopping": {"color": "#d6336c", "subs": [
        "Clothing & Shoes", "Electronics", "Books", "Gifts", "Hobbies & Crafts", "Office Supplies"]},
    "Entertainment": {"color": "#f08c00", "subs": ["Movies & Events", "Games", "Subscriptions & Streaming"]},
    "Bills & Utilities": {"color": "#495057", "subs": ["Phone", "Internet", "Electricity & Gas", "Water", "Insurance"]},
    "Kids": {"color": "#74b816", "subs": ["Baby Supplies", "Toys", "School Supplies", "Childcare"]},
    "Pets": {"color": "#9c36b5", "subs": ["Pet Food", "Pet Supplies", "Vet"]},
    "Travel": {"color": "#1098ad", "subs": ["Lodging", "Flights", "Travel Activities"]},
    "Education": {"color": "#3b5bdb", "subs": ["Courses & Tuition", "Exam Fees"]},
    "Fees & Taxes": {"color": "#868e96", "subs": ["Sales Tax", "Tip", "Service & Bag Fees", "Discount"]},
    "Other": {"color": "#adb5bd", "subs": ["Uncategorized"]},
}

CATEGORY_NAMES = list(TAXONOMY)
SUBCATEGORY_NAMES = sorted({s for v in TAXONOMY.values() for s in v["subs"]})


def is_valid(category: str, subcategory: str) -> bool:
    return category in TAXONOMY and subcategory in TAXONOMY[category]["subs"]


def rows():
    for cat, v in TAXONOMY.items():
        for sub in v["subs"]:
            yield cat, sub, v["color"]


def prompt_listing() -> str:
    return "\n".join(f"- {c}: {', '.join(v['subs'])}" for c, v in TAXONOMY.items())
