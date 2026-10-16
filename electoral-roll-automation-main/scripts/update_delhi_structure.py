import json

# Load existing structure
with open('C:/Users/pallav/Desktop/Python/PdfZipperMiniO/discovered_structure.json', 'r') as f:
    data = json.load(f)

# Create Delhi structure from the discovery output
delhi_structure = {
    'years': {
        '2026': {
            'roll_types': {
                'SIR DraftRoll - 2026': {
                    'districts': {
                        'Central': {
                            'constituencies': [
                                {'value': '23', 'label': '23 - KAROL BAGH'},
                                {'value': '24', 'label': '24 - PATEL NAGAR'},
                                {'value': '25', 'label': '25 - MOTI NAGAR'},
                                {'value': '39', 'label': '39 - RAJINDER NAGAR'}
                            ],
                            'languages': [
                                {'value': 'ENG', 'label': 'ENGLISH'},
                                {'value': 'HIN', 'label': 'HINDI'}
                            ]
                        },
                        'Central North': {
                            'constituencies': [
                                {'value': '14', 'label': '14 - SHALIMAR BAGH'},
                                {'value': '15', 'label': '15 - SHAKUR BASTI'},
                                {'value': '16', 'label': '16 - TRI NAGAR'},
                                {'value': '17', 'label': '17 - WAZIRPUR'},
                                {'value': '18', 'label': '18 - MODEL TOWN'}
                            ],
                            'languages': [
                                {'value': 'ENG', 'label': 'ENGLISH'},
                                {'value': 'HIN', 'label': 'HINDI'}
                            ]
                        },
                        'East': {
                            'constituencies': [
                                {'value': '55', 'label': '55 - TRILOKPURI'},
                                {'value': '56', 'label': '56 - KONDLI'},
                                {'value': '57', 'label': '57 - PATPARGANJ'},
                                {'value': '58', 'label': '58 - LAXMI NAGAR'},
                                {'value': '59', 'label': '59 - VISHWAS NAGAR'},
                                {'value': '60', 'label': '60 - KRISHNA NAGAR'},
                                {'value': '61', 'label': '61 - GANDHI NAGAR'},
                                {'value': '62', 'label': '62 - SHAHDARA'}
                            ],
                            'languages': [
                                {'value': 'ENG', 'label': 'ENGLISH'},
                                {'value': 'HIN', 'label': 'HINDI'}
                            ]
                        },
                        'New Delhi': {
                            'constituencies': [
                                {'value': '38', 'label': '38 - DELHI CANTT'},
                                {'value': '40', 'label': '40 - NEW DELHI'}
                            ],
                            'languages': [
                                {'value': 'ENG', 'label': 'ENGLISH'},
                                {'value': 'HIN', 'label': 'HINDI'}
                            ]
                        },
                        'North': {
                            'constituencies': [
                                {'value': '2', 'label': '2 - BURARI'},
                                {'value': '3', 'label': '3 - TIMARPUR'},
                                {'value': '4', 'label': '4 - ADARSH NAGAR'},
                                {'value': '5', 'label': '5 - BADLI'}
                            ],
                            'languages': [
                                {'value': 'ENG', 'label': 'ENGLISH'},
                                {'value': 'HIN', 'label': 'HINDI'}
                            ]
                        },
                        'North East': {
                            'constituencies': [
                                {'value': '63', 'label': '63 - SEEMA PURI'},
                                {'value': '64', 'label': '64 - ROHTAS NAGAR'},
                                {'value': '65', 'label': '65 - SEELAMPUR'},
                                {'value': '66', 'label': '66 - GHONDA'},
                                {'value': '67', 'label': '67 - BABARPUR'},
                                {'value': '68', 'label': '68 - GOKALPUR'},
                                {'value': '69', 'label': '69 - MUSTAFABAD'},
                                {'value': '70', 'label': '70 - KARAWAL NAGAR'}
                            ],
                            'languages': [
                                {'value': 'ENG', 'label': 'ENGLISH'},
                                {'value': 'HIN', 'label': 'HINDI'},
                                {'value': 'URD', 'label': 'URDU'}
                            ]
                        },
                        'North West': {
                            'constituencies': [
                                {'value': '6', 'label': '6 - RITHALA'},
                                {'value': '7', 'label': '7 - BAWANA'},
                                {'value': '8', 'label': '8 - MUNDKA'},
                                {'value': '9', 'label': '9 - KIRARI'},
                                {'value': '10', 'label': '10 - SULTANPUR MAJRA'},
                                {'value': '11', 'label': '11 - NANGLOI JAT'}
                            ],
                            'languages': [
                                {'value': 'ENG', 'label': 'ENGLISH'},
                                {'value': 'HIN', 'label': 'HINDI'}
                            ]
                        },
                        'Old Delhi': {
                            'constituencies': [
                                {'value': '19', 'label': '19 - SADAR BAZAR'},
                                {'value': '20', 'label': '20 - CHANDNI CHOWK'},
                                {'value': '21', 'label': '21 - MATIA MAHAL'},
                                {'value': '22', 'label': '22 - BALLIMARAN'}
                            ],
                            'languages': [
                                {'value': 'ENG', 'label': 'ENGLISH'},
                                {'value': 'HIN', 'label': 'HINDI'}
                            ]
                        },
                        'Outer North': {
                            'constituencies': [
                                {'value': '1', 'label': '1 - NERELA'},
                                {'value': '12', 'label': '12 - MANGOLPURI'},
                                {'value': '13', 'label': '13 - ROHINI'}
                            ],
                            'languages': [
                                {'value': 'ENG', 'label': 'ENGLISH'},
                                {'value': 'HIN', 'label': 'HINDI'}
                            ]
                        },
                        'South': {
                            'constituencies': [
                                {'value': '43', 'label': '43 - MALVIYA NAGAR'},
                                {'value': '44', 'label': '44 - R K PURAM'},
                                {'value': '45', 'label': '45 - MEHRAULI'},
                                {'value': '46', 'label': '46 - CHHATARPUR'},
                                {'value': '47', 'label': '47 - DEOLI'},
                                {'value': '48', 'label': '48 - AMBEDKAR NAGAR'},
                                {'value': '49', 'label': '49 - SANGAM VIHAR'}
                            ],
                            'languages': [
                                {'value': 'ENG', 'label': 'ENGLISH'},
                                {'value': 'HIN', 'label': 'HINDI'}
                            ]
                        },
                        'South East': {
                            'constituencies': [
                                {'value': '41', 'label': '41 - JANGPURA'},
                                {'value': '42', 'label': '42 - KASTURBA NAGAR'},
                                {'value': '50', 'label': '50 - GREATER KAILASH'},
                                {'value': '51', 'label': '51 - KALKAJI'},
                                {'value': '52', 'label': '52 - TUGHLAKABAD'},
                                {'value': '53', 'label': '53 - BADARPUR'},
                                {'value': '54', 'label': '54 - OKHLA'}
                            ],
                            'languages': [
                                {'value': 'ENG', 'label': 'ENGLISH'},
                                {'value': 'HIN', 'label': 'HINDI'}
                            ]
                        },
                        'South West': {
                            'constituencies': [
                                {'value': '33', 'label': '33 - DWARKA'},
                                {'value': '34', 'label': '34 - MATIALA'},
                                {'value': '35', 'label': '35 - NAJAFGARH'},
                                {'value': '36', 'label': '36 - BIJWASAN'},
                                {'value': '37', 'label': '37 - PALAM'}
                            ],
                            'languages': [
                                {'value': 'ENG', 'label': 'ENGLISH'},
                                {'value': 'HIN', 'label': 'HINDI'}
                            ]
                        },
                        'West': {
                            'constituencies': [
                                {'value': '26', 'label': '26 - MADIPUR'},
                                {'value': '27', 'label': '27 - RAJOURI GARDEN'},
                                {'value': '28', 'label': '28 - HARI NAGAR'},
                                {'value': '29', 'label': '29 - TILAK NAGAR'},
                                {'value': '30', 'label': '30 - JANAKPURI'},
                                {'value': '31', 'label': '31 - VIKASPURI'},
                                {'value': '32', 'label': '32 - UTTAM NAGAR'}
                            ],
                            'languages': [
                                {'value': 'ENG', 'label': 'ENGLISH'},
                                {'value': 'HIN', 'label': 'HINDI'}
                            ]
                        }
                    }
                }
            }
        },
        '2025': {
            'roll_types': {
                'Supplement-5 2025': {
                    'districts': {}
                },
                'State General Election Roll 2025': {
                    'districts': {}
                },
                'DraftRoll - 2025': {
                    'districts': {}
                },
                'Final Roll - 2025': {
                    'districts': {}
                }
            }
        }
    }
}

# Copy the same district structure for all 2025 roll types
for roll_type in delhi_structure['years']['2025']['roll_types']:
    delhi_structure['years']['2025']['roll_types'][roll_type]['districts'] = delhi_structure['years']['2026']['roll_types']['SIR DraftRoll - 2026']['districts']

# Update data
data['NCT OF Delhi'] = delhi_structure

# Save back
with open('C:/Users/pallav/Desktop/Python/PdfZipperMiniO/discovered_structure.json', 'w') as f:
    json.dump(data, f, indent=2)

print('Delhi structure updated successfully!')